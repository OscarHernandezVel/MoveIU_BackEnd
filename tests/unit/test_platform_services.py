"""Jobs, rate limiter, catalog loading, demo seed and health checks."""

from __future__ import annotations

import pytest

from core_service.clock import FakeClock
from core_service.metrics import MetricsRegistry
from core_service.models import GeoPoint, Stop
from core_service.persistence.documents import InMemoryDocumentStore
from core_service.persistence.relational import CatalogRepository, Database, Migrator
from core_service.routing import RouteCatalog
from core_service.security import SlidingWindowRateLimiter
from core_service.services.catalog_service import CatalogService
from core_service.services.demo_seed import load_demo_data
from core_service.services.health_service import HealthService
from core_service.services.jobs import JobRunner
from pg import make_database


class TestJobRunner:
    def make(self) -> tuple[JobRunner, FakeClock, MetricsRegistry]:
        clock, metrics = FakeClock(), MetricsRegistry()
        return JobRunner(clock, metrics), clock, metrics

    def test_jobs_run_when_due_and_then_wait_for_their_interval(self) -> None:
        runner, clock, _ = self.make()
        calls: list[str] = []
        runner.add("fast", 10, lambda: calls.append("fast"))
        runner.add("slow", 60, lambda: calls.append("slow"))
        assert runner.run_pending() == []
        clock.advance(10)
        assert runner.run_pending() == ["fast"]
        assert runner.run_pending() == []
        clock.advance(50)
        assert runner.run_pending() == ["fast", "slow"]
        assert calls == ["fast", "fast", "slow"]

    def test_run_immediately(self) -> None:
        runner, _, _ = self.make()
        runner.add("now", 100, lambda: None, run_immediately=True)
        assert runner.run_pending() == ["now"]

    def test_a_failing_job_is_isolated_counted_and_retried_later(self) -> None:
        runner, clock, metrics = self.make()
        order: list[str] = []

        def broken() -> None:
            raise RuntimeError("bug")

        job = runner.add("broken", 5, broken)
        runner.add("healthy", 5, lambda: order.append("ok"))
        clock.advance(5)
        assert runner.run_pending() == ["broken", "healthy"] and order == ["ok"]
        assert (job.failures, job.last_error) == (1, "RuntimeError")
        assert metrics.counter("job_failures", job="broken") == 1
        clock.advance(5)
        runner.run_pending()
        assert job.runs == 2

    def test_invalid_jobs_are_rejected(self) -> None:
        runner, _, _ = self.make()
        with pytest.raises(ValueError):
            runner.add("bad", 0, lambda: None)
        runner.add("dup", 1, lambda: None)
        with pytest.raises(ValueError):
            runner.add("dup", 1, lambda: None)

    def test_background_thread_starts_and_stops(self) -> None:
        runner, _, _ = self.make()
        runner.start(tick_seconds=0.01)
        runner.start(tick_seconds=0.01)  # idempotent
        runner.stop()
        runner.stop()


class TestRateLimiter:
    def test_allows_up_to_the_limit_then_blocks_with_retry_after(self) -> None:
        clock = FakeClock()
        limiter = SlidingWindowRateLimiter(3, 60, clock)
        decisions = [limiter.check("ip-1") for _ in range(4)]
        assert [d.allowed for d in decisions] == [True, True, True, False]
        assert decisions[0].remaining == 2 and decisions[2].remaining == 0
        assert decisions[3].retry_after_seconds == pytest.approx(60)

    def test_window_slides(self) -> None:
        clock = FakeClock()
        limiter = SlidingWindowRateLimiter(2, 60, clock)
        assert limiter.check("k").allowed
        clock.advance(30)
        assert limiter.check("k").allowed
        assert not limiter.check("k").allowed
        clock.advance(31)  # the first request left the window
        assert limiter.check("k").allowed
        assert not limiter.check("k").allowed

    def test_clients_are_independent(self) -> None:
        limiter = SlidingWindowRateLimiter(1, 60, FakeClock())
        assert limiter.check("a").allowed and limiter.check("b").allowed
        assert not limiter.check("a").allowed

    def test_least_recently_used_keys_are_evicted(self) -> None:
        limiter = SlidingWindowRateLimiter(1, 60, FakeClock(), max_keys=2)
        limiter.check("a")
        limiter.check("b")
        limiter.check("a")  # touch a: b is now the oldest
        limiter.check("c")
        assert len(limiter) == 2
        assert limiter.check("b").allowed  # b was forgotten
        assert not limiter.check("c").allowed  # c is still tracked

    def test_invalid_configuration(self) -> None:
        with pytest.raises(ValueError):
            SlidingWindowRateLimiter(0, 60, FakeClock())


def fresh_db() -> tuple[Database, CatalogRepository]:
    db = make_database()
    Migrator(db, FakeClock()).migrate()
    return db, CatalogRepository(db)


class TestCatalogService:
    def test_demo_data_is_loaded_into_the_catalog(self) -> None:
        _, repo = fresh_db()
        load_demo_data(repo)
        catalog = RouteCatalog()
        report = CatalogService(repo, catalog, MetricsRegistry()).reload()
        assert (report.routes_loaded, report.buses_loaded, report.invalid_routes) == (1, 3, [])
        assert catalog.route_ids() == ["C5"] and len(catalog.require_track("C5").stops()) == 6

    def test_seeding_twice_is_harmless(self) -> None:
        _, repo = fresh_db()
        load_demo_data(repo)
        load_demo_data(repo)
        assert len(repo.load_buses()) == 3

    def test_an_invalid_route_is_skipped_but_the_others_load(self) -> None:
        _, repo = fresh_db()
        load_demo_data(repo)
        repo.save_stop(Stop("P-900", "Solo", GeoPoint(1.0, -77.0)))
        repo.save_route("C9", "Broken", [GeoPoint(1.0, -77.0), GeoPoint(1.0, -77.0)], ["P-900"])  # zero-length path
        metrics = MetricsRegistry()
        report = CatalogService(repo, RouteCatalog(), metrics).reload()
        assert report.routes_loaded == 1 and report.invalid_routes == ["C9"]
        assert metrics.counter("catalog_invalid_routes") == 1


class TestHealth:
    def build(self, *, with_routes: bool = True) -> HealthService:
        clock = FakeClock()
        db, repo = fresh_db()
        catalog = RouteCatalog()
        if with_routes:
            load_demo_data(repo)
            CatalogService(repo, catalog, MetricsRegistry()).reload()
        return HealthService(db, InMemoryDocumentStore(clock), catalog, clock, lambda: {"enabled": False}, "1.0.0")

    def test_ready_when_everything_answers(self) -> None:
        health = self.build()
        report = health.readiness()
        assert report["status"] == "ready" and all(report["checks"].values()) and report["ai"] == {"enabled": False}
        assert health.liveness()["status"] == "up"

    def test_degraded_without_routes_or_with_a_broken_dependency(self) -> None:
        assert self.build(with_routes=False).readiness()["status"] == "degraded"
        health = self.build()
        health._database.close()  # noqa: SLF001
        report = health.readiness()
        assert report["status"] == "degraded" and report["checks"]["relational_database"] is False
