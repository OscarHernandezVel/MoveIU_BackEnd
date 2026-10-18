"""PostgreSQL layer: pool and transactions, atomic state changes, time series and TimescaleDB policy.

These tests run against a real PostgreSQL server (see ``tests/pg.py``); nothing is simulated except
the TimescaleDB *extension*, which most CI servers do not have (its statements are checked with a recorder).
"""

from __future__ import annotations

import threading
import time
from typing import Any, Optional, Sequence

import psycopg
import pytest

import pg
from core_service.clock import FakeClock
from core_service.errors import ConflictError, ServiceUnavailableError
from core_service.models import AlertStatus, Device, Direction, GeoPoint, ProximityAlert, Role, User
from core_service.persistence.relational import (
    MIGRATIONS,
    AlertRepository,
    CatalogRepository,
    Database,
    DeviceRepository,
    Migrator,
    TelemetryRepository,
    TimescaleManager,
    UserRepository,
)
from factories import demo_buses, demo_data, demo_stops, make_reading

NOW = 1_790_000_000.0


@pytest.fixture
def db() -> Database:
    return pg.make_database(FakeClock(NOW))


def seed_catalog(db: Database) -> None:
    catalog = CatalogRepository(db)
    for stop in demo_stops().values():
        catalog.save_stop(stop)
    route = demo_data()["routes"][0]
    catalog.save_route(
        route["route_id"], route["name"], [GeoPoint(lat, lng) for lng, lat in route["path"]], route["stops"]
    )
    for bus in demo_buses():
        catalog.save_bus(bus)


def make_alert(alert_id: str, expires_at: float = NOW + 3600) -> ProximityAlert:
    return ProximityAlert(alert_id, "device-1", "C5", Direction.OUTBOUND, "P-022", 2, NOW, expires_at)


@pytest.fixture
def alerts(db: Database) -> AlertRepository:
    seed_catalog(db)
    DeviceRepository(db).upsert(Device("device-1", "push-token-abcdef", "android"), NOW)
    return AlertRepository(db)


class TestPoolAndTransactions:
    def test_ping_and_pool_statistics(self, db: Database) -> None:
        assert db.ping() is True
        assert db.pool_stats()["pool_max"] >= db.pool_stats()["pool_min"] >= 1

    def test_an_unreachable_server_is_a_service_unavailable_error_not_a_driver_error(self) -> None:
        with pytest.raises(ServiceUnavailableError):
            Database("postgresql://nobody@127.0.0.1:1/none", connect_timeout_seconds=1, pool_timeout_seconds=1)

    def test_a_closed_database_reports_unavailable(self, db: Database) -> None:
        db.close()
        assert db.ping() is False
        with pytest.raises(ServiceUnavailableError):
            db.query_one("SELECT 1")

    def test_pool_sizes_are_validated(self) -> None:
        with pytest.raises(ValueError):
            Database(pg.ensure_server(), min_size=3, max_size=2)
        with pytest.raises(ValueError):
            Database(pg.ensure_server(), schema="bad; DROP SCHEMA public")

    def test_an_inner_failure_only_undoes_the_inner_block(self, db: Database) -> None:
        users = UserRepository(db)
        with db.transaction():
            users.create(User("u1", "Ana", "ana@example.com", None, "h", Role.PASSENGER, NOW))
            with pytest.raises(ConflictError), db.transaction():
                users.create(User("u2", "Ana 2", "ANA@example.com", None, "h", Role.PASSENGER, NOW))  # same e-mail
            users.create(User("u3", "Luis", "luis@example.com", None, "h", Role.PASSENGER, NOW))
        assert users.find_by_id("u1") and users.find_by_id("u3") and users.find_by_id("u2") is None

    def test_an_outer_failure_undoes_everything_including_committed_looking_inner_blocks(self, db: Database) -> None:
        users = UserRepository(db)
        with pytest.raises(RuntimeError), db.transaction():
            with db.transaction():
                users.create(User("u1", "Ana", "ana@example.com", None, "h", Role.PASSENGER, NOW))
            raise RuntimeError("boom")
        assert users.find_by_id("u1") is None

    def test_transactions_of_different_threads_use_different_connections(self, db: Database) -> None:
        users = UserRepository(db)
        inside, release = threading.Event(), threading.Event()

        def writer() -> None:
            with db.transaction():
                users.create(User("u1", "Ana", "ana@example.com", None, "h", Role.PASSENGER, NOW))
                inside.set()
                release.wait(3)

        thread = threading.Thread(target=writer)
        thread.start()
        assert inside.wait(3)
        assert users.find_by_id("u1") is None  # not visible before COMMIT (read committed)
        release.set()
        thread.join()
        assert users.find_by_id("u1") is not None

    def test_a_slow_statement_is_cut_by_the_server_and_not_retried(self) -> None:
        settings = pg.database_settings()
        database = Database(settings.url, schema=settings.schema, statement_timeout_ms=200)
        try:
            started = time.monotonic()
            with pytest.raises(ServiceUnavailableError):
                database.query_all("SELECT pg_sleep(3)")
            assert time.monotonic() - started < 1.5  # one attempt of ~0.2 s, not three
            assert database.ping()  # the connection is still usable
        finally:
            database.close()

    def test_user_input_is_never_interpreted_as_sql(self, db: Database) -> None:
        users = UserRepository(db)
        users.create(User("u1", "Ana", "ana@example.com", None, "h", Role.PASSENGER, NOW))
        for hostile in ("x' OR '1'='1", "'; DROP TABLE users; --", "ana@example.com' --", "%", "_"):
            assert users.find_by_email(hostile) is None
        assert users.find_by_email("ana@example.com") is not None


class TestSchemaRules:
    def test_e_mail_is_unique_ignoring_case(self, db: Database) -> None:
        users = UserRepository(db)
        users.create(User("u1", "Ana", "Ana@Example.com", None, "h", Role.PASSENGER, NOW))
        with pytest.raises(ConflictError):
            users.create(User("u2", "Otra", "ana@example.COM", None, "h", Role.PASSENGER, NOW))

    def test_constraints_are_enforced_by_the_database(self, db: Database) -> None:
        seed_catalog(db)
        with pytest.raises(psycopg.errors.CheckViolation):
            db.execute("INSERT INTO stops (stop_id, name, lat, lng) VALUES ('X', 'x', 95, 0)")
        with pytest.raises(psycopg.errors.ForeignKeyViolation):
            db.execute(
                "INSERT INTO buses (bus_id, route_id, model, seats, standing_positions) VALUES ('B', 'ZZ', 'm', 1, 1)"
            )
        with pytest.raises(psycopg.errors.CheckViolation):
            db.execute("UPDATE buses SET seats = 0 WHERE bus_id = '1045'")

    def test_the_partial_index_for_active_alerts_exists(self, db: Database) -> None:
        row = db.query_one("SELECT indexdef FROM pg_indexes WHERE indexname = 'idx_alerts_active'")
        assert row is not None and "status = 'active'" in row["indexdef"]

    def test_migrations_are_recorded_and_idempotent(self, db: Database) -> None:
        migrator = Migrator(db, FakeClock(NOW))
        assert migrator.applied_versions() == [m.version for m in MIGRATIONS] and migrator.migrate() == []

    def test_replicas_starting_together_apply_each_migration_exactly_once(self) -> None:
        settings = pg.database_settings()
        databases = [Database(settings.url, schema=settings.schema, min_size=1, max_size=2) for _ in range(4)]
        applied: list[list[int]] = []
        errors: list[BaseException] = []

        def start(database: Database) -> None:
            try:
                applied.append(Migrator(database, FakeClock(NOW)).migrate())
            except BaseException as error:  # noqa: BLE001
                errors.append(error)

        threads = [threading.Thread(target=start, args=(d,)) for d in databases]
        try:
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
            assert errors == []
            assert sorted(v for versions in applied for v in versions) == [
                m.version for m in MIGRATIONS
            ]  # nobody applied a version twice
            assert Migrator(databases[0]).applied_versions() == [m.version for m in MIGRATIONS]
        finally:
            for database in databases:
                database.close()


class TestAtomicAlertTransitions:
    def test_only_one_of_many_racing_deliveries_wins(self, alerts: AlertRepository) -> None:
        alerts.create(make_alert("a1"))
        wins: list[bool] = []
        barrier = threading.Barrier(8)

        def deliver() -> None:
            barrier.wait()
            wins.append(alerts.transition("a1", AlertStatus.ACTIVE, AlertStatus.SENT, sent_at=NOW))

        threads = [threading.Thread(target=deliver) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        assert wins.count(True) == 1 and alerts.find("a1").status is AlertStatus.SENT  # type: ignore[union-attr]

    def test_a_cancel_cannot_overwrite_a_delivery(self, alerts: AlertRepository) -> None:
        alerts.create(make_alert("a1"))
        assert alerts.transition("a1", AlertStatus.ACTIVE, AlertStatus.SENT, sent_at=NOW)
        assert alerts.cancel("a1", "device-1") is None
        assert alerts.find("a1").status is AlertStatus.SENT  # type: ignore[union-attr]

    def test_cancel_and_delivery_race_has_exactly_one_winner(self, alerts: AlertRepository) -> None:
        for round_number in range(15):
            alert_id = f"race-{round_number}"
            alerts.create(make_alert(alert_id))
            barrier, outcome = threading.Barrier(2), {}

            def cancel(alert_id: str = alert_id, barrier: threading.Barrier = barrier, outcome: dict = outcome) -> None:
                barrier.wait()
                outcome["cancel"] = alerts.cancel(alert_id, "device-1") is not None

            def deliver(
                alert_id: str = alert_id, barrier: threading.Barrier = barrier, outcome: dict = outcome
            ) -> None:
                barrier.wait()
                outcome["deliver"] = alerts.transition(alert_id, AlertStatus.ACTIVE, AlertStatus.SENT, sent_at=NOW)

            threads = [threading.Thread(target=cancel), threading.Thread(target=deliver)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
            assert outcome["cancel"] != outcome["deliver"], f"round {round_number}: {outcome}"
            final = alerts.find(alert_id).status  # type: ignore[union-attr]
            assert final is (AlertStatus.CANCELLED if outcome["cancel"] else AlertStatus.SENT)

    def test_expiry_returns_exactly_the_rows_it_expired_and_never_touches_delivered_ones(
        self, alerts: AlertRepository
    ) -> None:
        alerts.create(make_alert("due", expires_at=NOW + 10))
        alerts.create(make_alert("delivered", expires_at=NOW + 10))
        alerts.create(make_alert("later", expires_at=NOW + 999))
        alerts.transition("delivered", AlertStatus.ACTIVE, AlertStatus.SENT, sent_at=NOW)
        expired = alerts.expire_due(NOW + 20)
        assert [a.alert_id for a in expired] == ["due"] and expired[0].status is AlertStatus.EXPIRED
        assert alerts.find("delivered").status is AlertStatus.SENT  # type: ignore[union-attr]
        assert alerts.expire_due(NOW + 20) == []  # a second replica running the same job finds nothing


class TestTelemetrySeries:
    @pytest.fixture
    def telemetry(self, db: Database) -> TelemetryRepository:
        return TelemetryRepository(db)

    def save(self, telemetry: TelemetryRepository, bus: str, ts: float, seated: int, pct: int) -> None:
        telemetry.save(make_reading(bus_id=bus, ts=ts, seated=seated), pct)

    def test_timestamps_round_trip_as_epoch_seconds(self, telemetry: TelemetryRepository) -> None:
        reading = make_reading(ts=NOW + 0.123, approximate_count=True)
        assert telemetry.save(reading, 40) is True
        row = telemetry.recent("1045", 1)[0]
        assert isinstance(row["ts"], float) and row["ts"] == pytest.approx(NOW + 0.123, abs=1e-5)
        assert row["approximate_count"] is True and row["occupancy_pct"] == 40

    def test_replays_are_idempotent(self, telemetry: TelemetryRepository) -> None:
        reading = make_reading(ts=NOW)
        assert telemetry.save(reading, 10) is True and telemetry.save(reading, 10) is False
        assert telemetry.count() == 1

    def test_nearest_recent_and_prune(self, telemetry: TelemetryRepository) -> None:
        for offset in (0, 5, 10, 60):
            self.save(telemetry, "1045", NOW + offset, 10, 20)
        nearest = telemetry.nearest("1045", NOW + 6, tolerance_seconds=3)
        assert nearest is not None and nearest["ts"] == pytest.approx(NOW + 5)
        assert telemetry.nearest("1045", NOW + 30, tolerance_seconds=3) is None
        assert [round(r["ts"] - NOW) for r in telemetry.recent("1045", 2)] == [60, 10]
        assert telemetry.prune_older_than(NOW + 8) == 2 and telemetry.count() == 2

    def test_the_five_minute_view_summarises_each_bus(self, telemetry: TelemetryRepository) -> None:
        bucket = NOW - (NOW % 300)  # start of a 5-minute bucket
        for offset, pct in ((10, 20), (100, 40), (200, 60)):
            self.save(telemetry, "1045", bucket + offset, 10, pct)
        self.save(telemetry, "1045", bucket + 310, 10, 90)  # next bucket
        rows = telemetry.bus_trend("1045", bucket - 1)
        assert [r["bucket_ts"] for r in rows] == [bucket + 300, bucket]  # newest first
        first = rows[1]
        assert first["samples"] == 3 and first["avg_occupancy_pct"] == pytest.approx(40.0)
        assert (first["max_occupancy_pct"], first["min_occupancy_pct"]) == (60, 20)

    def test_the_route_trend_weights_buses_by_their_samples(self, db: Database, telemetry: TelemetryRepository) -> None:
        seed_catalog(db)
        bucket = NOW - (NOW % 300)
        for offset in (1, 2, 3):  # 3 samples at 30 %
            self.save(telemetry, "1045", bucket + offset, 10, 30)
        self.save(telemetry, "1102", bucket + 4, 10, 70)  # 1 sample at 70 %
        (row,) = telemetry.route_trend("C5", bucket - 1)
        assert row["buses"] == 2 and row["samples"] == 4
        assert row["avg_occupancy_pct"] == pytest.approx((3 * 30 + 70) / 4) and row["max_occupancy_pct"] == 70
        assert telemetry.route_trend("Z9", bucket - 1) == []

    def test_recent_occupancy_of_many_buses_takes_one_query(self, telemetry: TelemetryRepository) -> None:
        for offset in range(6):
            self.save(telemetry, "1045", NOW + offset, 10, 10 + offset)
            self.save(telemetry, "1102", NOW + offset, 10, 50 + offset)
        result = telemetry.recent_occupancy_many(["1045", "1102", "9999"], per_bus=3, since=NOW - 10)
        assert result == {"1045": [15, 14, 13], "1102": [55, 54, 53]}
        assert telemetry.recent_occupancy_many([], 3, NOW) == {}
        assert telemetry.recent_occupancy_many(["1045"], 3, since=NOW + 100) == {}  # bounded time window


class Recorder:
    """A Database stand-in that records statements and answers the catalogue queries of TimescaleManager."""

    def __init__(
        self, *, extension: bool, hypertable: bool, aggregate_kind: Optional[str] = None, aggregate: bool = False
    ) -> None:
        self.statements: list[str] = []
        self._extension, self._hypertable, self._kind = extension, hypertable, aggregate_kind
        self._aggregate = aggregate

    def query_one(self, sql: str, params: Sequence[Any] = ()) -> Optional[dict[str, Any]]:
        if "pg_extension" in sql:
            return {"ok": 1} if self._extension else None
        if "hypertables" in sql:
            return {"ok": 1} if self._hypertable else None
        if "continuous_aggregates" in sql:
            return {"ok": 1} if self._aggregate else None
        if "pg_class" in sql:
            return None if self._kind is None else {"kind": self._kind}
        raise AssertionError(sql)

    def execute(self, sql: str, params: Sequence[Any] = ()) -> int:
        self.statements.append(" ".join(sql.split()) + (f" {tuple(params)}" if params else ""))
        return 0


class TestTimescalePolicy:
    def test_on_plain_postgres_the_five_minute_view_is_kept_and_the_mode_is_reported(self, db: Database) -> None:
        status = TimescaleManager(db).configure()
        assert status.enabled is False and status.mode == "postgresql-plain"
        assert db.query_one("SELECT count(*) AS n FROM telemetry_5min")["n"] == 0  # type: ignore[index]
        assert TimescaleManager(db).configure().enabled is False  # idempotent

    def test_invalid_policy_values_are_rejected(self, db: Database) -> None:
        for retention, compress in ((0, 1), (10, 10), (10, 20), (5, 0)):
            with pytest.raises(ValueError):
                TimescaleManager(db).configure(retention, compress)

    def test_with_the_extension_it_sets_compression_retention_and_a_continuous_aggregate(self) -> None:
        recorder = Recorder(extension=True, hypertable=True)
        status = TimescaleManager(recorder).configure(retention_days=30, compress_after_days=7)  # type: ignore[arg-type]
        assert status.enabled and (status.retention_days, status.compress_after_days) == (30, 7)
        text = "\n".join(recorder.statements)
        assert "timescaledb.compress_segmentby = 'bus_id'" in text
        assert "add_compression_policy('telemetry_history', make_interval(days => %s)) (7,)" in text
        assert "add_retention_policy(%s::regclass, make_interval(days => %s)) ('telemetry_history', 30)" in text
        assert "CREATE MATERIALIZED VIEW telemetry_5min WITH (timescaledb.continuous" in text
        assert "add_continuous_aggregate_policy" in text and "WITH NO DATA" in text
        assert "('telemetry_5min', 365)" in text  # aggregates outlive the raw rows
        # policies are removed before being added again, so re-running never fails or stacks policies
        assert text.index("remove_compression_policy") < text.index("add_compression_policy")

    def test_a_view_left_by_a_run_without_the_extension_is_replaced_by_the_aggregate(self) -> None:
        recorder = Recorder(extension=True, hypertable=True, aggregate_kind="v")
        TimescaleManager(recorder).configure()  # type: ignore[arg-type]
        text = "\n".join(recorder.statements)
        assert text.index("DROP VIEW telemetry_5min") < text.index("CREATE MATERIALIZED VIEW telemetry_5min")

    def test_an_existing_aggregate_is_not_recreated_nor_dropped(self) -> None:
        # In pg_class a continuous aggregate is a *view* (relkind 'v'): running the configuration a second time
        # must recognise it through TimescaleDB and leave it alone (it used to be dropped as a "plain view").
        recorder = Recorder(extension=True, hypertable=True, aggregate_kind="v", aggregate=True)
        TimescaleManager(recorder).configure()  # type: ignore[arg-type]
        text = "\n".join(recorder.statements)
        assert "CREATE MATERIALIZED VIEW" not in text and "DROP VIEW" not in text
        assert "add_continuous_aggregate_policy" in text  # the policy is still re-applied

    def test_extension_without_hypertable_falls_back_to_the_plain_view(self) -> None:
        recorder = Recorder(extension=True, hypertable=False)
        assert TimescaleManager(recorder).configure().enabled is False  # type: ignore[arg-type]
        assert any(s.startswith("CREATE OR REPLACE VIEW telemetry_5min") for s in recorder.statements)
