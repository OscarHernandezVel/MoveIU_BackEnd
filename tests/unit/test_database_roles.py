"""Least-privilege roles: the SQL in ``deploy/postgres`` is executed for real and its promises checked.

The scripts are applied to a throw-away database with suffixed role names (roles are cluster-wide, so
suffixes keep the test from touching real ones). Needs a superuser test connection: otherwise skipped.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote, urlsplit, urlunsplit

import psycopg
import pytest
from psycopg import errors as pg_errors

import pg
from core_service.clock import FakeClock
from core_service.container import ApplicationContext
from core_service.persistence.relational import Database, Migrator

DEPLOY = Path(__file__).resolve().parents[2] / "deploy" / "postgres"
ROLES = ("core_migrator", "core_app", "core_reader")


@dataclass
class Cluster:
    admin_url: str
    database: str
    suffix: str
    password: str

    def role(self, name: str) -> str:
        return f"{name}_{self.suffix}"

    def url(self, name: str) -> str:
        parts = urlsplit(self.admin_url)
        host = parts.hostname or "127.0.0.1"
        netloc = f"{quote(self.role(name))}:{quote(self.password)}@{host}:{parts.port or 5432}"
        return urlunsplit((parts.scheme, netloc, f"/{self.database}", "", ""))

    def admin_of_database(self) -> str:
        parts = urlsplit(self.admin_url)
        return urlunsplit((parts.scheme, parts.netloc, f"/{self.database}", "", ""))


def _script(name: str, cluster: Cluster) -> str:
    text = (DEPLOY / name).read_text(encoding="utf-8")
    for role in ROLES:
        text = text.replace(role, cluster.role(role))
    for marker in ("__MIGRATOR_PASSWORD__", "__APP_PASSWORD__", "__READER_PASSWORD__"):
        text = text.replace(marker, cluster.password)
    return text


@pytest.fixture
def cluster() -> Iterator[Cluster]:
    admin_url = pg.ensure_server()
    with psycopg.connect(admin_url, autocommit=True) as admin:
        row = admin.execute("SELECT rolsuper FROM pg_roles WHERE rolname = current_user").fetchone()
        if not row or not row[0]:
            pytest.skip("hace falta un superusuario para crear roles y bases de datos de prueba")
        suffix = uuid.uuid4().hex[:8]
        database = f"roles_{suffix}"
        admin.execute(f'CREATE DATABASE "{database}"')
    env = Cluster(admin_url, database, suffix, "test-only-" + uuid.uuid4().hex)
    try:
        with psycopg.connect(env.admin_of_database(), autocommit=True) as connection:
            connection.execute(_script("01-roles.sql", env))
        yield env
    finally:
        with psycopg.connect(admin_url, autocommit=True) as admin:
            admin.execute(f'DROP DATABASE IF EXISTS "{database}" WITH (FORCE)')
            for role in ROLES:
                admin.execute(f'DROP ROLE IF EXISTS "{env.role(role)}"')


@pytest.fixture
def migrated(cluster: Cluster) -> Cluster:
    """Schema created by the migrator role; reader grants applied afterwards, as documented."""
    owner = Database(cluster.url("core_migrator"), min_size=1, max_size=1)
    try:
        Migrator(owner, FakeClock()).migrate()
    finally:
        owner.close()
    with psycopg.connect(cluster.url("core_migrator"), autocommit=True) as connection:
        connection.execute(_script("02-reader-grants.sql", cluster))
    return cluster


def _denied(connection: psycopg.Connection[tuple[object, ...]], sql: str) -> bool:
    try:
        connection.execute(sql)
    except (pg_errors.InsufficientPrivilege, pg_errors.ReadOnlySqlTransaction):
        return True
    return False


class TestRoleSeparation:
    def test_the_application_role_can_read_and_write_data_but_not_change_the_schema(self, migrated: Cluster) -> None:
        with psycopg.connect(migrated.url("core_app"), autocommit=True) as app:
            app.execute("INSERT INTO stops (stop_id, name, lat, lng) VALUES ('X-1', 'Uno', 1.0, -77.0)")
            assert app.execute("SELECT count(*) FROM stops WHERE stop_id = 'X-1'").fetchone() == (1,)
            app.execute("UPDATE stops SET name = 'Dos' WHERE stop_id = 'X-1'")
            app.execute("DELETE FROM stops WHERE stop_id = 'X-1'")
            assert _denied(app, "CREATE TABLE evil (id int)")
            assert _denied(app, "DROP TABLE stops")
            assert _denied(app, "ALTER TABLE stops ADD COLUMN evil text")
            assert _denied(app, "TRUNCATE stops")
            assert _denied(app, "CREATE ROLE intruder")

    def test_the_application_role_cannot_rewrite_the_migration_history(self, migrated: Cluster) -> None:
        with psycopg.connect(migrated.url("core_app"), autocommit=True) as app:
            assert _denied(app, "DROP TABLE schema_migrations") is True

    def test_the_reader_sees_fleet_data_only(self, migrated: Cluster) -> None:
        with psycopg.connect(migrated.url("core_reader"), autocommit=True) as reader:
            for table in ("telemetry_history", "telemetry_5min", "routes", "stops", "route_stops", "buses"):
                reader.execute(f"SELECT * FROM {table} LIMIT 1")  # noqa: S608 - fixed table names
            for table in ("users", "devices", "alerts", "schema_migrations"):
                assert _denied(reader, f"SELECT * FROM {table} LIMIT 1"), table  # noqa: S608 - fixed table names

    def test_the_reader_cannot_see_who_audited(self, migrated: Cluster) -> None:
        with psycopg.connect(migrated.url("core_reader"), autocommit=True) as reader:
            reader.execute("SELECT sample_id, manual_seated, sampled_at FROM audit_samples LIMIT 1")
            assert _denied(reader, "SELECT auditor_id FROM audit_samples LIMIT 1")
            assert _denied(reader, "SELECT * FROM audit_samples LIMIT 1")

    def test_the_reader_cannot_write_even_where_it_can_read(self, migrated: Cluster) -> None:
        with psycopg.connect(migrated.url("core_reader"), autocommit=True) as reader:
            assert _denied(reader, "INSERT INTO stops (stop_id, name, lat, lng) VALUES ('X', 'X', 1, 1)")
            assert _denied(reader, "DELETE FROM buses")
            with pytest.raises((pg_errors.ReadOnlySqlTransaction, pg_errors.InsufficientPrivilege)):
                reader.execute("CREATE TEMP TABLE scratch (id int)")

    def test_outsiders_cannot_connect_at_all(self, cluster: Cluster) -> None:
        parts = urlsplit(cluster.admin_url)
        with psycopg.connect(cluster.admin_of_database(), autocommit=True) as admin:
            admin.execute("CREATE ROLE outsider_" + cluster.suffix + " LOGIN PASSWORD 'x'")
        outsider = urlunsplit(
            (
                parts.scheme,
                f"outsider_{cluster.suffix}:x@{parts.hostname}:{parts.port or 5432}",
                f"/{cluster.database}",
                "",
                "",
            )
        )
        try:
            with pytest.raises(psycopg.OperationalError, match="CONNECT privilege"), psycopg.connect(outsider):
                pass
        finally:
            with psycopg.connect(cluster.admin_url, autocommit=True) as admin:
                admin.execute(f'DROP OWNED BY "outsider_{cluster.suffix}"')
                admin.execute(f'DROP ROLE "outsider_{cluster.suffix}"')


class TestSeparateMigrationRole:
    def test_the_service_runs_with_a_role_that_has_no_ddl_rights(self, cluster: Cluster) -> None:
        from core_service.config import DatabaseSettings, Settings

        settings = Settings.for_testing(
            database=DatabaseSettings(
                url=cluster.url("core_app"),
                migration_url=cluster.url("core_migrator"),
                schema="",
                pool_min=1,
                pool_max=2,
            )
        )
        context = ApplicationContext.create(settings, clock=FakeClock(1_790_000_000.0), seed_demo=True)
        pg.on_release(context.close)
        assert context.database.ping()
        assert context.route_catalog.route_ids() == ["C5"]
        with pytest.raises(Exception, match="permission denied|ServiceUnavailable|denied"):
            context.database.execute("CREATE TABLE should_not_exist (id int)")
