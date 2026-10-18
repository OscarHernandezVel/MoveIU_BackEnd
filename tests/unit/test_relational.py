"""Relational layer: migrations, constraints, repositories and transactions."""

from __future__ import annotations

import psycopg
import pytest

from core_service.clock import FakeClock
from core_service.errors import ConflictError
from core_service.models import (
    AlertStatus,
    AuditSample,
    Device,
    Direction,
    Finding,
    FindingKind,
    FindingSeverity,
    ProximityAlert,
    Role,
    User,
)
from core_service.persistence.relational import (
    AlertRepository,
    AuditRepository,
    CatalogRepository,
    Database,
    DeviceRepository,
    FindingRepository,
    Migrator,
    TelemetryRepository,
    UserRepository,
)
from factories import demo_buses, demo_data, demo_stops, make_reading
from pg import make_database

NOW = 1_790_000_000.0


@pytest.fixture
def db() -> Database:
    database = make_database()
    Migrator(database, FakeClock(NOW)).migrate()
    return database


@pytest.fixture
def seeded(db: Database) -> Database:
    catalog = CatalogRepository(db)
    for stop in demo_stops().values():
        catalog.save_stop(stop)
    from core_service.models import GeoPoint

    route = demo_data()["routes"][0]
    catalog.save_route(
        route["route_id"], route["name"], [GeoPoint(lat, lng) for lng, lat in route["path"]], route["stops"]
    )
    for bus in demo_buses():
        catalog.save_bus(bus)
    return db


class TestMigrations:
    def test_migrate_is_idempotent(self, db: Database) -> None:
        migrator = Migrator(db, FakeClock(NOW))
        assert migrator.migrate() == []
        assert migrator.applied_versions() == [1, 2, 3]

    def test_a_failing_migration_rolls_back(self) -> None:
        from core_service.persistence.relational import migrations

        database = make_database(migrate=False)
        broken = migrations.Migration(99, "broken", ("CREATE TABLE ok_table (id INTEGER)", "THIS IS NOT SQL"))
        original = migrations.MIGRATIONS
        migrations.MIGRATIONS = (*original, broken)
        try:
            with pytest.raises(psycopg.errors.SyntaxError):
                Migrator(database, FakeClock(NOW)).migrate()
            ghost = database.query_one("SELECT to_regclass('ok_table') AS name")
            assert ghost is not None and ghost["name"] is None  # the CREATE TABLE was rolled back too
            assert 99 not in Migrator(database, FakeClock(NOW)).applied_versions()
        finally:
            migrations.MIGRATIONS = original


class TestTransactions:
    def test_rollback_on_error(self, db: Database) -> None:
        users = UserRepository(db)
        with pytest.raises(RuntimeError), db.transaction():
            users.create(User("u1", "Ana", "ana@example.com", None, "hash", Role.PASSENGER, NOW))
            raise RuntimeError("boom")
        assert users.find_by_id("u1") is None

    def test_nested_transactions_join_the_outer_one(self, db: Database) -> None:
        users = UserRepository(db)
        with pytest.raises(RuntimeError), db.transaction():
            with db.transaction():
                users.create(User("u1", "Ana", "ana@example.com", None, "hash", Role.PASSENGER, NOW))
            raise RuntimeError("outer failure")
        assert users.find_by_id("u1") is None


class TestUsersAndDevices:
    def test_create_and_lookup_case_insensitive_email(self, db: Database) -> None:
        users = UserRepository(db)
        users.create(User("u1", "Ana", "Ana@Example.com", "+573001112233", "hash", Role.PASSENGER, NOW))
        found = users.find_by_email("ana@example.COM")
        assert found is not None and found.user_id == "u1" and found.role is Role.PASSENGER

    def test_duplicate_email_is_a_conflict(self, db: Database) -> None:
        users = UserRepository(db)
        users.create(User("u1", "Ana", "ana@example.com", None, "hash", Role.PASSENGER, NOW))
        with pytest.raises(ConflictError):
            users.create(User("u2", "Ana 2", "ANA@example.com", None, "hash", Role.PASSENGER, NOW))

    def test_role_constraint_and_change(self, db: Database) -> None:
        users = UserRepository(db)
        users.create(User("u1", "Ana", "ana@example.com", None, "hash", Role.PASSENGER, NOW))
        assert users.set_role("u1", Role.AUDITOR) is True
        found = users.find_by_id("u1")
        assert found is not None and found.role is Role.AUDITOR
        with pytest.raises(ValueError):
            users.set_role("u1", Role.GUEST)
        with pytest.raises(psycopg.errors.IntegrityError):
            db.execute("UPDATE users SET role = 'superuser' WHERE user_id = 'u1'")

    def test_device_upsert_update_and_link(self, db: Database) -> None:
        users, devices = UserRepository(db), DeviceRepository(db)
        users.create(User("u1", "Ana", "ana@example.com", None, "hash", Role.PASSENGER, NOW))
        devices.upsert(Device("d1", "token-1", "android"), NOW)
        devices.upsert(Device("d1", "token-2", "android"), NOW + 1)
        found = devices.find("d1")
        assert found is not None and found.push_token == "token-2" and found.user_id is None
        assert devices.update_push_token("d1", "token-3", NOW + 2) is True
        assert devices.link_user("d1", "u1", NOW + 3) is True
        linked = devices.find("d1")
        assert linked is not None and linked.user_id == "u1" and linked.push_token == "token-3"
        assert devices.find("missing") is None
        assert devices.update_push_token("missing", "x", NOW) is False

    def test_sql_injection_attempts_are_treated_as_data(self, db: Database) -> None:
        users = UserRepository(db)
        users.create(User("u1", "Ana", "ana@example.com", None, "hash", Role.PASSENGER, NOW))
        assert users.find_by_email("' OR '1'='1") is None
        assert users.find_by_email("x'; DROP TABLE users; --") is None
        assert users.find_by_id("u1") is not None


class TestCatalogRepository:
    def test_round_trip_of_routes_and_buses(self, seeded: Database) -> None:
        catalog = CatalogRepository(seeded)
        routes = catalog.load_routes()
        assert [r.route_id for r in routes] == ["C5"]
        assert [s.stop_id for s in routes[0].stops] == ["P-001", "P-009", "P-018", "P-022", "P-027", "P-031"]
        assert len(routes[0].path) == 7
        buses = catalog.load_buses()
        assert [b.bus_id for b in buses] == ["1045", "1088", "1102"]
        found = catalog.find_bus("1102")
        assert found is not None and found.capacity.wheelchair_spaces == 1
        assert catalog.find_bus("nope") is None

    def test_save_route_replaces_the_stop_list(self, seeded: Database) -> None:
        catalog = CatalogRepository(seeded)
        route = catalog.load_routes()[0]
        catalog.save_route("C5", "Renamed", route.path, ["P-001", "P-031"])
        reloaded = catalog.load_routes()[0]
        assert reloaded.name == "Renamed"
        assert [s.stop_id for s in reloaded.stops] == ["P-001", "P-031"]

    def test_inactive_routes_are_not_loaded(self, seeded: Database) -> None:
        catalog = CatalogRepository(seeded)
        route = catalog.load_routes()[0]
        catalog.save_route("C5", route.name, route.path, [s.stop_id for s in route.stops], active=False)
        assert catalog.load_routes() == []

    def test_foreign_keys_are_enforced(self, seeded: Database) -> None:
        with pytest.raises(psycopg.errors.IntegrityError):
            seeded.execute("INSERT INTO route_stops (route_id, stop_order, stop_id) VALUES ('C5', 99, 'NOPE')")


class TestAlertRepository:
    @staticmethod
    def _alert(alert_id: str = "a1", expires: float = NOW + 3600) -> ProximityAlert:
        return ProximityAlert(alert_id, "d1", "C5", Direction.OUTBOUND, "P-031", 2, NOW, expires)

    @pytest.fixture
    def alerts(self, seeded: Database) -> AlertRepository:
        DeviceRepository(seeded).upsert(Device("d1", "t", "android"), NOW)
        return AlertRepository(seeded)

    def test_create_find_and_list_active(self, alerts: AlertRepository) -> None:
        alerts.create(self._alert("a1"))
        alerts.create(self._alert("a2", expires=NOW - 1))  # already expired
        assert alerts.find("a1") is not None
        assert alerts.count_active("d1", NOW) == 1
        assert [a.alert_id for a in alerts.list_active_for_device("d1", NOW)] == ["a1"]
        assert [a.alert_id for a in alerts.list_all_active(NOW)] == ["a1"]

    def test_transition_is_compare_and_set(self, alerts: AlertRepository) -> None:
        alerts.create(self._alert())
        assert alerts.transition("a1", AlertStatus.ACTIVE, AlertStatus.SENT, sent_at=NOW + 5) is True
        assert alerts.transition("a1", AlertStatus.ACTIVE, AlertStatus.SENT) is False  # second winner impossible
        stored = alerts.find("a1")
        assert stored is not None and stored.status is AlertStatus.SENT and stored.sent_at == NOW + 5
        assert alerts.transition("a1", AlertStatus.SENT, AlertStatus.ACTIVE) is True  # rollback path
        reverted = alerts.find("a1")
        assert reverted is not None and reverted.sent_at is None and reverted.status is AlertStatus.ACTIVE

    def test_cancel_only_by_the_owner(self, alerts: AlertRepository) -> None:
        alerts.create(self._alert())
        assert alerts.cancel("a1", "other-device") is None
        cancelled = alerts.cancel("a1", "d1")
        assert cancelled is not None and cancelled.status is AlertStatus.CANCELLED
        assert alerts.cancel("a1", "d1") is None

    def test_expire_due(self, alerts: AlertRepository) -> None:
        alerts.create(self._alert("a1", expires=NOW + 10))
        alerts.create(self._alert("a2", expires=NOW + 100))
        expired = alerts.expire_due(NOW + 50)
        assert [a.alert_id for a in expired] == ["a1"]
        stored = alerts.find("a1")
        assert stored is not None and stored.status is AlertStatus.EXPIRED
        assert alerts.expire_due(NOW + 50) == []


class TestFindingsAuditAndTelemetry:
    def test_findings(self, db: Database) -> None:
        findings = FindingRepository(db)
        findings.create(Finding("f1", "1045", FindingKind.SILENT_BUS, FindingSeverity.HIGH, "silent", "rules", NOW))
        findings.create(Finding("f2", None, FindingKind.AI_RECOMMENDATION, FindingSeverity.LOW, "ai", "ai", NOW + 1))
        assert [f.finding_id for f in findings.list_recent()] == ["f2", "f1"]
        assert [f.finding_id for f in findings.list_recent(source="ai")] == ["f2"]
        assert [f.finding_id for f in findings.list_unnotified_since(NOW)] == ["f1", "f2"]
        findings.mark_notified(["f1"], NOW + 5)
        assert [f.finding_id for f in findings.list_unnotified_since(NOW)] == ["f2"]

    def test_audit_samples_join_the_route(self, seeded: Database) -> None:
        UserRepository(seeded).create(User("a1", "Aud", "aud@example.com", None, "hash", Role.AUDITOR, NOW))
        audits = AuditRepository(seeded)
        sample = AuditSample("s1", "1045", "P-018", "a1", 30, 6, 31, 6, 38, 0.95, "occ-1.2", NOW)
        audits.add(sample)
        rows = audits.list_between(NOW - 1, NOW + 1)
        assert len(rows) == 1 and rows[0][0] == sample and rows[0][1] == "C5"
        assert audits.list_between(NOW - 1, NOW + 1, route_id="C1") == []
        assert audits.list_between(NOW + 1, NOW + 2) == []
        assert sample.manual_total == 36 and sample.model_total == 37

    def test_telemetry_is_idempotent_and_prunable(self, db: Database) -> None:
        telemetry = TelemetryRepository(db)
        reading = make_reading(ts=NOW)
        assert telemetry.save(reading, 55) is True
        assert telemetry.save(reading, 55) is False
        telemetry.save(make_reading(ts=NOW + 3), 60)
        assert telemetry.count() == 2
        nearest = telemetry.nearest("1045", NOW + 2, tolerance_seconds=5)
        assert nearest is not None and nearest["ts"] == NOW + 3
        assert telemetry.nearest("1045", NOW + 500) is None
        assert [r["ts"] for r in telemetry.recent("1045", 1)] == [NOW + 3]
        assert telemetry.prune_older_than(NOW + 1) == 1
        assert telemetry.count() == 1
