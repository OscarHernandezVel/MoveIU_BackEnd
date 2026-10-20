"""Reliability: restarts keep the data, failures are contained and the system degrades gracefully."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from fastapi.testclient import TestClient

from core_service.api import create_app
from core_service.clock import FakeClock
from core_service.config import Settings
from core_service.container import ApplicationContext
from core_service.models import Role
from fakes import RecordingNotifier, RecordingPushGateway
from pg import make_settings
from system_harness import API, INGEST, System, build_system


def durable_settings(tmp_path: Path) -> Settings:
    base = make_settings()
    return replace(base, document_store_path=str(tmp_path / "documents.log"))


def boot(settings: Settings, clock: FakeClock) -> tuple[ApplicationContext, TestClient]:
    context = ApplicationContext.create(
        settings,
        clock=clock,
        push_gateway=RecordingPushGateway(),
        team_notifier=RecordingNotifier(),
        sleep=lambda _s: None,
        seed_demo=True,
    )
    return context, TestClient(create_app(context), raise_server_exceptions=False)


class TestRestart:
    def test_alerts_inbox_sessions_and_live_state_survive_a_restart(self, tmp_path: Path) -> None:
        settings, clock = durable_settings(tmp_path), FakeClock(1_790_000_000.0)
        first, client = boot(settings, clock)
        pair = client.post(
            f"{API}/auth/guest", json={"device_id": "device-aaaa-0001", "platform": "android", "push_token": "t"}
        ).json()
        headers = {"Authorization": f"Bearer {pair['access_token']}"}
        client.post(
            f"{API}/alerts", json={"route_id": "C5", "direction": "outbound", "stop_id": "P-022"}, headers=headers
        )
        client.post(
            f"{API}/alerts",
            json={"route_id": "C5", "direction": "outbound", "stop_id": "P-031", "stops_before": 1},
            headers=headers,
        )
        track = first.route_catalog.require_track("C5")
        for progress in (0.20, 0.30, 0.40, 0.45):  # fires the first alert
            point = track.path.point_at(progress)
            reading = {
                "ts": clock.now(),
                "lat": point.lat,
                "lng": point.lng,
                "seated": 10,
                "standing": 0,
                "doors_total": 10,
                "confidence": 0.9,
                "model_version": "occ-1.2",
                "speed_kmh": 30.0,
            }
            assert client.post(f"{API}/internal/buses/1045/telemetry", json=reading, headers=INGEST).status_code == 200
            clock.advance(5)
        assert client.get(f"{API}/notifications", headers=headers).json()["unread"] == 1
        first.close()

        second, client = boot(settings, clock)  # a new process on the same files
        try:
            assert [a["stop_id"] for a in client.get(f"{API}/alerts", headers=headers).json()["alerts"]] == ["P-031"]
            assert client.get(f"{API}/notifications", headers=headers).json()["unread"] == 1
            assert second.live_states.get("1045") is not None
            assert client.post(f"{API}/auth/refresh", json={"refresh_token": pair["refresh_token"]}).status_code == 200
            # the rebuilt in-memory index still drives the surviving alert
            track = second.route_catalog.require_track("C5")
            for progress in (0.80, 0.90, 0.95):
                point = track.path.point_at(progress)
                reading = {
                    "ts": clock.now(),
                    "lat": point.lat,
                    "lng": point.lng,
                    "seated": 10,
                    "standing": 0,
                    "doors_total": 10,
                    "confidence": 0.9,
                    "model_version": "occ-1.2",
                    "speed_kmh": 30.0,
                }
                client.post(f"{API}/internal/buses/1102/telemetry", json=reading, headers=INGEST)
                clock.advance(5)
            assert client.get(f"{API}/notifications", headers=headers).json()["unread"] == 2
        finally:
            second.close()


class TestGracefulDegradation:
    def test_a_failing_event_subscriber_does_not_break_ingestion(self, system: System) -> None:
        def broken(_event: object) -> None:
            raise RuntimeError("bug in a subscriber")

        from core_service.services.events import OccupancyUpdated

        system.context.events.subscribe(OccupancyUpdated, broken)
        assert system.ingest("1045", 0.3).status_code == 200
        assert system.context.live_states.get("1045") is not None
        assert system.context.metrics.counter("event_handler_errors", event="OccupancyUpdated") == 1

    def test_unexpected_errors_return_a_generic_message_without_internals(self, system: System) -> None:
        def explode(*_a: object, **_k: object) -> None:
            raise RuntimeError("secret internal detail: /var/lib/db")

        system.context.route_catalog.tracks = explode  # type: ignore[method-assign]
        response = system.client.get(f"{API}/routes", headers=system.guest())
        assert response.status_code == 500
        assert "secret" not in response.text and response.json()["error"]["code"] == "internal_error"

    def test_a_broken_job_does_not_stop_the_others(self, system: System) -> None:
        system.context.jobs.add("exploding", 10, lambda: 1 / 0)
        system.ingest("1045", 0.3)
        system.clock.advance(120)
        executed = system.context.jobs.run_pending()
        assert "exploding" in executed and "stale_buses" in executed
        assert system.context.live_states.get("1045").state.value == "no_data"  # type: ignore[union-attr]
        assert system.context.metrics.counter("job_failures", job="exploding") == 1

    def test_readiness_turns_degraded_when_the_database_is_gone(self, system: System) -> None:
        system.context.database.close()
        response = system.client.get(f"{API}/health/ready")
        assert response.status_code == 503 and response.json()["status"] == "degraded"
        assert system.client.get(f"{API}/health/live").status_code == 200

    def test_an_invalid_route_in_the_database_does_not_stop_the_service(self, tmp_path: Path) -> None:
        settings, clock = durable_settings(tmp_path), FakeClock()
        first, _ = boot(settings, clock)
        from core_service.models import GeoPoint, Stop

        first.catalog_repository.save_stop(Stop("P-900", "Solo", GeoPoint(1.0, -77.0)))
        first.catalog_repository.save_route("C9", "Roto", [GeoPoint(1.0, -77.0), GeoPoint(1.0, -77.0)], ["P-900"])
        first.close()
        second, client = boot(settings, clock)
        try:
            assert second.route_catalog.route_ids() == ["C5"]
            assert client.get(f"{API}/health/ready").status_code == 200
        finally:
            second.close()

    def test_old_telemetry_is_pruned_by_the_retention_job(self, system: System) -> None:
        system.ingest("1045", 0.3)
        system.clock.advance(31 * 86_400)
        system.context.jobs.run_pending()
        assert system.context.telemetry.count() == 0

    def test_the_weekly_audit_report_is_sent_once_on_monday_morning(self) -> None:
        system = build_system()
        admin = system.staff(Role.ADMIN, "admin@example.com")
        system.ingest("1045", 0.4, seated=30, doors_total=30)
        system.client.post(
            f"{API}/audits",
            json={"bus_id": "1045", "stop_id": "P-018", "manual_seated": 31, "manual_standing": 0},
            headers=admin,
        )
        system.clock.advance(3_600)
        system.context.jobs.run_pending()
        system.clock.advance(3_600)
        system.context.jobs.run_pending()
        reports = [m for m in system.notifier.messages if "Resumen semanal" in m[0] or "semanal" in m[0].lower()]
        assert len(reports) == 1  # 2026-09-21 is a Monday after 07:00 local: sent once, not on every hourly run
