"""Several service replicas on ONE PostgreSQL schema and ONE Redis: the behaviour that horizontal scaling needs.

Each "replica" is a complete application (own memory, own HTTP client, own push gateway); what they
share is exactly what production shares: PostgreSQL and Redis.
"""

from __future__ import annotations

import time
from dataclasses import replace
from typing import Any

import fakeredis
import pytest

import pg
from core_service.clock import FakeClock
from core_service.config import Settings
from system_harness import API, PASSWORD, System, build_system

pytestmark = pytest.mark.filterwarnings("ignore::pytest.PytestUnraisableExceptionWarning")


def attempt(system: System) -> int:
    """A cheap unauthenticated request that is not exempt from the request limiter."""
    return system.client.get(f"{API}/routes").status_code  # 401 without a token; the limiter runs before auth


class Cluster:
    """Two replicas sharing settings (same database schema), clock and Redis."""

    def __init__(self, settings: Settings | None = None) -> None:
        self.server = fakeredis.FakeServer()
        self.redis = fakeredis.FakeRedis(server=self.server, decode_responses=True)
        self.clock = FakeClock(1_790_000_000.0)
        shared = settings or pg.make_settings()
        self.a = build_system(shared, ai_enabled=False, clock=self.clock, redis_client=self.redis)
        self.b = build_system(shared, ai_enabled=False, clock=self.clock, redis_client=self.redis)

    def wait_for(self, condition: Any, seconds: float = 3.0) -> bool:
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            if condition():
                return True
            time.sleep(0.02)
        return False


@pytest.fixture
def cluster() -> Cluster:
    return Cluster()


def create_alert(system: System, headers: dict[str, str], **extra: object) -> Any:
    body = {"route_id": "C5", "direction": "outbound", "stop_id": "P-022", "stops_before": 2, **extra}
    return system.client.post(f"{API}/alerts", json=body, headers=headers)


class TestSharedAlertsAndInbox:
    def test_an_alert_created_on_one_replica_fires_from_readings_handled_by_the_other(self, cluster: Cluster) -> None:
        a, b = cluster.a, cluster.b
        replicators = [a.context.index_replicator, b.context.index_replicator]
        assert all(replicators)
        for replicator in replicators:
            replicator.start()  # type: ignore[union-attr]  # what the application lifespan does
        try:
            time.sleep(0.3)  # give the subscribers a moment to connect
            headers = a.guest()
            assert create_alert(a, headers).status_code == 201
            assert cluster.wait_for(lambda: len(b.context.alerts._index) == 1)  # noqa: SLF001 - pushed within ms

            b.drive("1045", 0.20, 0.45, steps=6)  # the bus talks ONLY to replica B

            assert len(b.push.sent) == 1 and len(a.push.sent) == 0
            inbox = a.client.get(f"{API}/notifications", headers=headers).json()  # ...and replica A serves the inbox
            assert inbox["unread"] == 1 and inbox["items"][0]["kind"] == "proximity"
            assert a.client.get(f"{API}/alerts", headers=headers).json()["alerts"] == []
            assert cluster.wait_for(lambda: len(a.context.alerts._index) == 0)  # noqa: SLF001 - removal broadcast back
        finally:
            for replicator in replicators:
                replicator.stop()  # type: ignore[union-attr]

    def test_reconciliation_from_the_database_finds_alerts_when_a_broadcast_was_missed(self, cluster: Cluster) -> None:
        a, b = cluster.a, cluster.b
        headers = a.guest()
        assert create_alert(a, headers).status_code == 201
        b.drive("1045", 0.20, 0.30, steps=3)
        assert len(b.push.sent) == 0  # B never heard about the alert (no subscription running)
        assert b.context.alerts.rebuild_index() == 1  # the periodic reconciliation job
        b.drive("1045", 0.35, 0.45, steps=3)
        assert len(b.push.sent) == 1

    def test_an_alert_is_delivered_once_even_if_both_replicas_see_the_bus_at_the_same_time(
        self, cluster: Cluster
    ) -> None:
        a, b = cluster.a, cluster.b
        headers = a.guest()
        create_alert(a, headers)
        b.context.alerts.rebuild_index()
        for progress in (0.20, 0.30, 0.40, 0.44, 0.45):
            a.ingest("1045", progress)
            b.ingest("1102", progress)  # another bus, handled by the other replica
            cluster.clock.advance(5)
        # bus 1045 (A) and bus 1102 (B) both approach the stop: the SQL compare-and-set lets only one notification out
        total = len(a.push.sent) + len(b.push.sent)
        assert total == 1
        assert a.client.get(f"{API}/notifications", headers=headers).json()["unread"] == 1

    def test_the_inbox_is_the_same_on_every_replica(self, cluster: Cluster) -> None:
        a, b = cluster.a, cluster.b
        headers = a.guest()
        create_alert(a, headers)
        a.context.alerts.rebuild_index()
        a.drive("1045", 0.20, 0.45, steps=6)
        item = b.client.get(f"{API}/notifications", headers=headers).json()["items"][0]
        assert b.client.post(f"{API}/notifications/{item['notification_id']}/read", headers=headers).status_code == 204
        assert a.client.get(f"{API}/notifications", headers=headers).json()["unread"] == 0  # read on B, seen on A


class TestSharedLiveState:
    def test_a_reading_handled_by_one_replica_is_visible_from_the_other(self, cluster: Cluster) -> None:
        a, b = cluster.a, cluster.b
        headers = b.guest()
        a.ingest("1045", 0.4, seated=20, doors_total=20)
        body = b.client.get(f"{API}/buses/active?route_id=C5", headers=headers).json()
        assert body["routes"][0]["buses"][0]["occupancy"]["percentage"] == 41  # 20 / 49
        assert cluster.redis.exists("core:doc:live_state:1045") == 1
        assert cluster.redis.smembers("core:set:live_route_index:C5") == {"1045"}

    def test_a_bus_that_alternates_between_replicas_keeps_one_continuous_history(self, cluster: Cluster) -> None:
        """Cameras and doors disagree for 35 s. The verdict needs the whole run, wherever each reading landed."""
        replicas = [cluster.a, cluster.b]
        for step in range(8):
            response = replicas[step % 2].ingest("1045", 0.3, seated=20, standing=0, doors_total=26)
            assert response.status_code == 200
            cluster.clock.advance(5)
        state = cluster.a.context.live_states.get("1045")
        assert state is not None and state.approximate_count  # sustained disagreement detected across replicas

    def test_late_readings_are_recognised_by_whichever_replica_receives_them(self, cluster: Cluster) -> None:
        cluster.a.ingest("1045", 0.4, seated=10)
        late = cluster.b.ingest("1045", 0.3, ts_offset=-30, seated=25)
        assert late.status_code == 200 and late.json()["status"] == "late_stored"
        state = cluster.b.context.live_states.get("1045")
        assert state is not None and state.seated == 10

    def test_only_one_replica_processes_the_same_bus_at_a_time(self, cluster: Cluster) -> None:
        import threading

        results: list[int] = []

        def send(system: System, offset: float) -> None:
            results.append(system.ingest("1045", 0.4, ts_offset=offset, seated=12).status_code)

        threads = [threading.Thread(target=send, args=(s, float(i))) for i, s in enumerate([cluster.a, cluster.b] * 3)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        assert results == [200] * 6
        assert cluster.a.context.telemetry.count() == 6  # every reading stored exactly once


class TestClusterWideSecurity:
    def test_login_lockout_applies_on_every_replica(self, cluster: Cluster) -> None:
        a, b = cluster.a, cluster.b
        a.staff(__import__("core_service.models", fromlist=["Role"]).Role.PASSENGER, "ana@example.com")
        for system in (a, b, a):  # three failures spread over two replicas
            wrong = system.client.post(
                f"{API}/auth/login", json={"email": "ana@example.com", "password": "Wr0ngPassword"}
            )
            assert wrong.status_code == 401
        locked_b = b.client.post(f"{API}/auth/login", json={"email": "ana@example.com", "password": PASSWORD})
        locked_a = a.client.post(f"{API}/auth/login", json={"email": "ana@example.com", "password": PASSWORD})
        assert locked_a.status_code == locked_b.status_code == 429
        cluster.clock.advance(301)
        assert (
            b.client.post(f"{API}/auth/login", json={"email": "ana@example.com", "password": PASSWORD}).status_code
            == 200
        )

    def test_the_request_quota_is_shared(self) -> None:
        base = pg.make_settings()
        limited = replace(base, security=replace(base.security, rate_limit_per_minute=4))
        cluster = Cluster(limited)
        statuses = [attempt(system) for system in (cluster.a, cluster.b, cluster.a, cluster.b, cluster.a, cluster.b)]
        assert statuses == [401, 401, 401, 401, 429, 429]  # 4 requests in total, whichever replica served them

    def test_refresh_tokens_are_valid_on_any_replica_and_single_use(self, cluster: Cluster) -> None:
        a, b = cluster.a, cluster.b
        pair = a.client.post(
            f"{API}/auth/guest", json={"device_id": "device-aaaa-0001", "platform": "android", "push_token": "t"}
        ).json()
        refreshed = b.client.post(f"{API}/auth/refresh", json={"refresh_token": pair["refresh_token"]})
        assert refreshed.status_code == 200
        replay = a.client.post(f"{API}/auth/refresh", json={"refresh_token": pair["refresh_token"]})
        assert replay.status_code == 401  # rotated on B: A must refuse the old token


class TestClusterCoordination:
    def test_each_periodic_job_runs_on_one_replica_only(self, cluster: Cluster) -> None:
        cluster.clock.advance(120)
        ran_a = set(cluster.a.context.jobs.run_pending())
        ran_b = set(cluster.b.context.jobs.run_pending())
        assert ran_a and not (ran_a & ran_b)  # the second replica finds every lease taken
        assert ran_a | ran_b >= {"stale_buses", "alert_expiry"}


class TestRedisOutage:
    def test_the_service_degrades_and_recovers_without_a_restart(self, cluster: Cluster) -> None:
        system = cluster.a
        headers = system.guest()
        assert system.ingest("1045", 0.3).status_code == 200

        cluster.server.connected = False  # Redis disappears
        assert system.ingest("1045", 0.31, ts_offset=1).status_code == 503  # the edge device keeps its data and retries
        assert system.client.get(f"{API}/health/ready").json()["status"] == "degraded"
        assert system.client.get(f"{API}/health/live").status_code == 200  # the process itself is fine
        routes = system.client.get(f"{API}/routes", headers=headers)
        assert routes.status_code == 200  # the static catalogue is served from memory: no Redis needed

        cluster.server.connected = True  # Redis comes back
        assert system.ingest("1045", 0.32, ts_offset=2).status_code == 200
        assert system.client.get(f"{API}/health/ready").json()["status"] == "ready"

    def test_the_request_limiter_keeps_protecting_the_api_while_redis_is_down(self) -> None:
        base = pg.make_settings()
        cluster = Cluster(replace(base, security=replace(base.security, rate_limit_per_minute=3)))
        cluster.server.connected = False
        codes = [attempt(cluster.a) for _ in range(5)]
        assert codes == [401, 401, 401, 429, 429]  # local fallback limiter, per replica
