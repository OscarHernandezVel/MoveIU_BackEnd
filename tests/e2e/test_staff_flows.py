"""Accuracy audits, technical findings and the AI analysis, as the technical team uses them."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from core_service.ai import LLMError
from core_service.models import Role
from fakes import text_reply, tool_reply
from pg import make_settings
from system_harness import API, INGEST, System, build_system

ANSWER = {
    "summary": "Se detecta un bus con baja confianza.",
    "findings": [
        {
            "recommended_action": "review_camera",
            "affected_route": "C5",
            "detected_anomaly": "low_model_confidence",
            "severity": "medium",
            "affected_bus_id": "1045",
            "confidence": 0.8,
            "justification": "Confianza del conteo por debajo de 0,6.",
        }
    ],
}


class TestAudits:
    def test_an_auditor_compares_the_manual_count_with_the_model(self, system: System) -> None:
        auditor = system.staff(Role.AUDITOR, "aud@example.com")
        system.ingest("1045", 0.40, seated=30, standing=6, doors_total=36)
        created = system.client.post(
            f"{API}/audits",
            json={"bus_id": "1045", "stop_id": "P-018", "manual_seated": 31, "manual_standing": 6},
            headers=auditor,
        )
        assert created.status_code == 201
        body = created.json()
        assert body["manual"]["total"] == 37 and body["model"]["total"] == 36 and body["absolute_error"] == 1

        today = datetime.fromtimestamp(system.clock.now(), timezone(timedelta(hours=-5))).date().isoformat()
        metrics = system.client.get(f"{API}/audits/metrics", params={"start": today, "end": today}, headers=auditor)
        assert metrics.status_code == 200 and metrics.json()["total_samples"] == 1

    def test_csv_upload_reports_good_and_bad_rows(self, system: System) -> None:
        auditor = system.staff(Role.AUDITOR, "aud@example.com")
        system.ingest("1045", 0.40, seated=20, standing=2, doors_total=22)
        stamp = datetime.fromtimestamp(system.clock.now(), timezone.utc).isoformat().replace("+00:00", "Z")
        text = (
            f"bus_id,stop_id,timestamp,manual_seated,manual_standing\n1045,P-018,{stamp},20,2\n9999,P-018,{stamp},1,1\n"
        )
        response = system.client.post(
            f"{API}/audits/csv", content=text, headers={**auditor, "Content-Type": "text/csv"}
        )
        assert response.status_code == 200
        assert response.json()["processed"] == 1 and response.json()["rejected"][0]["line"] == 3

    def test_only_audit_staff_can_use_audits(self, system: System) -> None:
        guest = system.guest()
        passenger = system.client.post(
            f"{API}/auth/register", json={"name": "Ana", "email": "ana@example.com", "password": "Str0ngPassw0rd"}
        ).json()
        body = {"bus_id": "1045", "stop_id": "P-018", "manual_seated": 1, "manual_standing": 1}
        assert system.client.post(f"{API}/audits", json=body, headers=guest).status_code == 403
        as_passenger = {"Authorization": f"Bearer {passenger['access_token']}"}
        assert system.client.post(f"{API}/audits", json=body, headers=as_passenger).status_code == 403
        assert (
            system.client.get(f"{API}/audits/metrics?start=2026-01-01&end=2026-01-02", headers=guest).status_code == 403
        )
        assert system.client.post(f"{API}/audits", json=body).status_code == 401

    def test_audit_input_is_validated(self, system: System) -> None:
        auditor = system.staff(Role.AUDITOR, "aud@example.com")
        bad = {"bus_id": "1045", "stop_id": "P-018", "manual_seated": -1, "manual_standing": 1}
        assert system.client.post(f"{API}/audits", json=bad, headers=auditor).status_code == 422
        assert (
            system.client.get(f"{API}/audits/metrics?start=2026-02-02&end=2026-01-01", headers=auditor).status_code
            == 422
        )
        assert system.client.get(f"{API}/audits/metrics?start=yesterday&end=today", headers=auditor).status_code == 422
        assert system.client.post(f"{API}/audits/csv", content=b"\xff\xfe\x00bad", headers=auditor).status_code == 422

    def test_labelled_samples_feed_the_training_dataset_for_the_ai(self, system: System) -> None:
        admin = system.staff(Role.ADMIN, "admin@example.com")
        system.ingest("1045", 0.40, seated=30, standing=6, doors_total=36)
        system.client.post(
            f"{API}/audits",
            json={"bus_id": "1045", "stop_id": "P-018", "manual_seated": 31, "manual_standing": 6},
            headers=admin,
        )
        summary = system.client.get(f"{API}/ai/training-dataset/summary", headers=admin).json()
        assert summary == {"samples_by_model_version": {"occ-1.2": 1}}
        export = system.client.get(f"{API}/ai/training-dataset", headers=admin)
        assert export.headers["content-type"].startswith("application/x-ndjson")
        lines = [json.loads(line) for line in export.text.splitlines()]
        assert lines[0]["labels"] == {"seated": 31, "standing": 6} and "email" not in export.text
        assert (
            system.client.get(
                f"{API}/ai/training-dataset", headers=system.staff(Role.AUDITOR, "aud@example.com")
            ).status_code
            == 403
        )


class TestAIAnalysis:
    def prepared(self, script: list) -> tuple[System, dict]:
        system = build_system(llm_script=script)
        admin = system.staff(Role.ADMIN, "admin@example.com")
        system.ingest("1045", 0.40, seated=20, doors_total=20)
        return system, admin

    def test_the_model_answer_becomes_findings(self) -> None:
        system, admin = self.prepared([tool_reply("list_route_buses", {"route_id": "C5"}), text_reply(ANSWER)])
        response = system.client.post(
            f"{API}/ai/analysis", json={"query": "revisa la ruta C5", "route_ids": ["C5"]}, headers=admin
        )
        assert response.status_code == 200
        report = response.json()
        assert (
            report["mode"] == "ai"
            and report["recorded_findings"] == 1
            and report["findings"][0]["affected_bus_id"] == "1045"
        )
        findings = system.client.get(f"{API}/findings?source=ai", headers=admin).json()["findings"]
        assert len(findings) == 1 and findings[0]["kind"] == "ai_recommendation"
        runs = system.client.get(f"{API}/ai/runs", headers=admin).json()["runs"]
        assert runs[0]["run_id"] == report["run_id"] and runs[0]["model"] == "fake-model"

    def test_nothing_personal_is_sent_to_the_model(self) -> None:
        system, admin = self.prepared([text_reply(ANSWER)])
        system.guest()  # a device with a push token exists in the system
        system.client.post(f"{API}/ai/analysis", json={"query": "estado general"}, headers=admin)
        sent = json.dumps(system.llm.requests)
        for secret in ("push-token-1", "device-aaaa-0001", "admin@example.com", "user-admin"):
            assert secret not in sent

    def test_when_the_provider_fails_the_rules_answer_instead(self) -> None:
        system, admin = self.prepared([LLMError("provider down")])
        system.clock.advance(300)  # bus 1045 has been silent for a while
        admin = system.staff(Role.AUDITOR, "aud2@example.com")
        report = system.client.post(f"{API}/ai/analysis", json={}, headers=admin).json()
        assert report["mode"] == "rules" and report["degraded_reason"] == "provider_error_retryable"
        assert report["findings"][0]["detected_anomaly"] == "bus_offline"

    def test_an_invalid_model_answer_is_never_trusted(self) -> None:
        bad = {
            "summary": "x",
            "findings": [{"recommended_action": "format_disk", "affected_route": "C5", "detected_anomaly": "none"}],
        }
        system, admin = self.prepared([text_reply(bad), text_reply(bad)])
        report = system.client.post(f"{API}/ai/analysis", json={}, headers=admin).json()
        assert report["mode"] == "rules" and report["degraded_reason"] == "invalid_answer"

    def test_ai_disabled_still_gives_a_rule_based_analysis(self) -> None:
        system = build_system(ai_enabled=False)
        admin = system.staff(Role.ADMIN, "admin@example.com")
        assert (
            system.client.post(f"{API}/ai/analysis", json={}, headers=admin).json()["degraded_reason"] == "ai_disabled"
        )

    def test_request_validation_and_permissions(self) -> None:
        system, admin = self.prepared([text_reply(ANSWER)])
        assert system.client.post(f"{API}/ai/analysis", json={"route_ids": ["Z9"]}, headers=admin).status_code == 422
        assert system.client.post(f"{API}/ai/analysis", json={"radius_km": 500}, headers=admin).status_code == 422
        assert (
            system.client.post(f"{API}/ai/analysis", json={"query": "avisa a ana@example.com"}, headers=admin).json()[
                "error"
            ]["code"]
            == "privacy_violation"
        )
        assert system.client.post(f"{API}/ai/analysis", json={}, headers=system.guest()).status_code == 403
        assert (
            system.client.get(f"{API}/ai/runs", headers=system.staff(Role.AUDITOR, "aud@example.com")).status_code
            == 403
        )
        assert system.llm.requests == []  # rejected requests never reached the model


class TestIngestionAndMonitoring:
    def test_the_hardware_needs_the_api_key(self, system: System) -> None:
        body = system.reading(0.3)
        url = f"{API}/internal/buses/1045/telemetry"
        assert system.client.post(url, json=body).status_code == 401
        assert system.client.post(url, json=body, headers={"X-Ingest-Key": "wrong"}).status_code == 401
        assert system.client.post(url, json=body, headers={"Authorization": "Bearer x"}).status_code == 401
        assert system.client.post(url, json=body, headers=INGEST).json() == {"status": "applied"}
        assert system.client.post(url, json=body, headers=INGEST).json() == {"status": "duplicate"}

    def test_ingestion_is_disabled_when_no_key_is_configured(self) -> None:
        from dataclasses import replace

        base = make_settings()
        system = build_system(replace(base, security=replace(base.security, ingest_key="")))
        assert (
            system.client.post(
                f"{API}/internal/buses/1045/telemetry", json=system.reading(0.3), headers=INGEST
            ).status_code
            == 503
        )

    def test_invalid_telemetry_is_rejected_without_side_effects(self, system: System) -> None:
        url = f"{API}/internal/buses/1045/telemetry"
        for field, value in (("seated", -1), ("confidence", 1.5), ("lat", 95), ("ts", 0), ("model_version", "")):
            assert (
                system.client.post(url, json={**system.reading(0.3), field: value}, headers=INGEST).status_code == 422
            )
        assert system.client.post(url, json={**system.reading(0.3), "extra": 1}, headers=INGEST).status_code == 422
        assert (
            system.client.post(
                f"{API}/internal/buses/9999/telemetry", json=system.reading(0.3), headers=INGEST
            ).status_code
            == 404
        )
        assert system.client.post(url, json=system.reading(0.3, ts_offset=3_600), headers=INGEST).status_code == 422
        assert system.context.telemetry.count() == 0

    def test_a_buffered_burst_is_processed_in_order_with_partial_failures(self, system: System) -> None:
        readings = [
            {**system.reading(0.30, ts_offset=-40, seated=5), "bus_id": "1045"},
            {**system.reading(0.20, ts_offset=-60, seated=3), "bus_id": "1045"},
            {**system.reading(0.25, ts_offset=-50, seated=4), "bus_id": "9999"},
            {**system.reading(0.35, ts_offset=-20, seated=9), "bus_id": "1045"},
        ]
        response = system.client.post(f"{API}/internal/telemetry/batch", json={"readings": readings}, headers=INGEST)
        body = response.json()
        assert response.status_code == 200 and (body["accepted"], body["rejected"]) == (3, 1)
        assert [i["status"] for i in body["items"]] == ["applied", "applied", "rejected", "applied"]
        assert body["items"][2]["code"] == "bus_not_found"
        state = system.context.live_states.get("1045")
        assert state is not None and state.seated == 9  # the newest one wins regardless of arrival order
        assert (
            system.client.post(f"{API}/internal/telemetry/batch", json={"readings": []}, headers=INGEST).status_code
            == 422
        )

    def test_health_and_metrics(self, system: System) -> None:
        assert system.client.get(f"{API}/health/live").json()["status"] == "up"
        assert system.client.get(f"{API}/health/ready").json()["status"] == "ready"
        assert system.client.get(f"{API}/metrics").status_code == 401
        system.ingest("1045", 0.3)
        metrics = system.client.get(f"{API}/metrics", headers=system.staff(Role.ADMIN, "admin@example.com")).json()
        assert metrics["counters"]["readings{status=applied}"] == 1
