"""Versioned schema migrations for PostgreSQL (+ TimescaleDB when the extension is present).

Each migration runs inside one transaction together with its bookkeeping row, so a failed
migration leaves the database untouched. Never edit an applied migration: add a new one.

Several service replicas may start at the same time: a transaction-level advisory lock makes
them apply the migrations one after the other, and each replica re-checks which versions are
already applied *after* taking the lock.

Time columns hold epoch seconds (``DOUBLE PRECISION``) everywhere except ``telemetry_history``,
whose ``ts`` is ``TIMESTAMPTZ`` because TimescaleDB partitions hypertables by a real time type.
The repository converts to and from epoch seconds, so the rest of the code never sees the difference.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from core_service.clock import Clock, SystemClock
from core_service.persistence.relational.database import Database
from core_service.persistence.relational.timescale import PLAIN_VIEW_SQL

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Migration:
    version: int
    description: str
    statements: tuple[str, ...]


_V1 = Migration(
    1,
    "initial schema",
    (
        """CREATE TABLE users (
            user_id       TEXT PRIMARY KEY,
            name          TEXT NOT NULL,
            email         TEXT NOT NULL,
            phone         TEXT UNIQUE,
            password_hash TEXT NOT NULL,
            role          TEXT NOT NULL DEFAULT 'passenger' CHECK (role IN ('passenger', 'auditor', 'admin')),
            created_at    DOUBLE PRECISION NOT NULL
        )""",
        """CREATE TABLE devices (
            device_id  TEXT PRIMARY KEY,
            user_id    TEXT REFERENCES users (user_id) ON DELETE SET NULL,
            push_token TEXT NOT NULL,
            platform   TEXT NOT NULL CHECK (platform IN ('android', 'ios')),
            created_at DOUBLE PRECISION NOT NULL,
            updated_at DOUBLE PRECISION NOT NULL
        )""",
        "CREATE UNIQUE INDEX uq_users_email ON users (lower(email))",
        "CREATE INDEX idx_devices_user ON devices (user_id)",
        """CREATE TABLE routes (
            route_id  TEXT PRIMARY KEY,
            name      TEXT NOT NULL,
            active    BOOLEAN NOT NULL DEFAULT TRUE,
            path_json TEXT NOT NULL
        )""",
        """CREATE TABLE stops (
            stop_id TEXT PRIMARY KEY,
            name    TEXT NOT NULL,
            lat     DOUBLE PRECISION NOT NULL CHECK (lat BETWEEN -90 AND 90),
            lng     DOUBLE PRECISION NOT NULL CHECK (lng BETWEEN -180 AND 180),
            active  BOOLEAN NOT NULL DEFAULT TRUE
        )""",
        """CREATE TABLE route_stops (
            route_id   TEXT NOT NULL REFERENCES routes (route_id) ON DELETE CASCADE,
            stop_order INTEGER NOT NULL CHECK (stop_order > 0),
            stop_id    TEXT NOT NULL REFERENCES stops (stop_id),
            PRIMARY KEY (route_id, stop_order),
            UNIQUE (route_id, stop_id)
        )""",
        """CREATE TABLE buses (
            bus_id               TEXT PRIMARY KEY,
            route_id             TEXT REFERENCES routes (route_id),
            model                TEXT NOT NULL,
            seats                INTEGER NOT NULL CHECK (seats > 0),
            standing_positions   INTEGER NOT NULL CHECK (standing_positions >= 0),
            wheelchair_spaces    INTEGER NOT NULL DEFAULT 0 CHECK (wheelchair_spaces >= 0),
            door_zones           INTEGER NOT NULL DEFAULT 2 CHECK (door_zones >= 0),
            status               TEXT NOT NULL DEFAULT 'inactive'
                                 CHECK (status IN ('active', 'inactive', 'maintenance'))
        )""",
        """CREATE TABLE alerts (
            alert_id      TEXT PRIMARY KEY,
            device_id     TEXT NOT NULL REFERENCES devices (device_id) ON DELETE CASCADE,
            route_id      TEXT NOT NULL REFERENCES routes (route_id),
            direction     TEXT NOT NULL CHECK (direction IN ('outbound', 'return')),
            stop_id       TEXT NOT NULL REFERENCES stops (stop_id),
            bus_id        TEXT REFERENCES buses (bus_id),
            stops_before  INTEGER NOT NULL CHECK (stops_before BETWEEN 1 AND 5),
            status        TEXT NOT NULL DEFAULT 'active'
                          CHECK (status IN ('active', 'sent', 'expired', 'cancelled')),
            created_at    DOUBLE PRECISION NOT NULL,
            expires_at    DOUBLE PRECISION NOT NULL,
            sent_at       DOUBLE PRECISION
        )""",
        "CREATE INDEX idx_alerts_active ON alerts (expires_at) WHERE status = 'active'",
        "CREATE INDEX idx_alerts_device ON alerts (device_id, status)",
        """CREATE TABLE technical_findings (
            finding_id  TEXT PRIMARY KEY,
            bus_id      TEXT,
            kind        TEXT NOT NULL,
            severity    TEXT NOT NULL CHECK (severity IN ('low', 'medium', 'high')),
            description TEXT NOT NULL,
            source      TEXT NOT NULL DEFAULT 'rules' CHECK (source IN ('rules', 'ai')),
            status      TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'reviewed')),
            notified_at DOUBLE PRECISION,
            created_at  DOUBLE PRECISION NOT NULL
        )""",
        "CREATE INDEX idx_findings_created ON technical_findings (created_at)",
        """CREATE TABLE audit_samples (
            sample_id       TEXT PRIMARY KEY,
            bus_id          TEXT NOT NULL REFERENCES buses (bus_id),
            stop_id         TEXT NOT NULL REFERENCES stops (stop_id),
            auditor_id      TEXT NOT NULL REFERENCES users (user_id),
            manual_seated   INTEGER NOT NULL CHECK (manual_seated >= 0),
            manual_standing INTEGER NOT NULL CHECK (manual_standing >= 0),
            model_seated    INTEGER NOT NULL,
            model_standing  INTEGER NOT NULL,
            doors_total     INTEGER NOT NULL,
            confidence      DOUBLE PRECISION NOT NULL,
            model_version   TEXT NOT NULL,
            sampled_at      DOUBLE PRECISION NOT NULL
        )""",
        "CREATE INDEX idx_audit_bus_time ON audit_samples (bus_id, sampled_at)",
        """CREATE TABLE telemetry_history (
            bus_id                   TEXT NOT NULL,
            ts                       TIMESTAMPTZ NOT NULL,
            lat                      DOUBLE PRECISION NOT NULL,
            lng                      DOUBLE PRECISION NOT NULL,
            seated                   INTEGER NOT NULL,
            standing                 INTEGER NOT NULL,
            standing_out_of_position INTEGER NOT NULL,
            doors_total              INTEGER NOT NULL,
            confidence               DOUBLE PRECISION NOT NULL,
            approximate_count        BOOLEAN NOT NULL,
            occupancy_pct            INTEGER NOT NULL,
            model_version            TEXT NOT NULL,
            PRIMARY KEY (bus_id, ts)
        )""",
        "CREATE INDEX idx_telemetry_ts ON telemetry_history (ts DESC)",
    ),
)

#: Turns ``telemetry_history`` into a hypertable when TimescaleDB is installed. On plain
#: PostgreSQL the block does nothing and the table stays an ordinary (indexed) table, so
#: development machines and CI work without the extension. Production settings require it.
_V2_HYPERTABLE = """
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_extension WHERE extname = 'timescaledb')
       AND EXISTS (SELECT 1 FROM pg_available_extensions WHERE name = 'timescaledb') THEN
        CREATE EXTENSION timescaledb;
    END IF;
    IF EXISTS (SELECT 1 FROM pg_extension WHERE extname = 'timescaledb') THEN
        PERFORM create_hypertable(
            'telemetry_history', 'ts',
            chunk_time_interval => INTERVAL '1 day',
            create_default_indexes => FALSE,
            if_not_exists => TRUE,
            migrate_data => TRUE
        );
    END IF;
END
$$
"""

#: ``telemetry_5min`` always exists: a plain view here, replaced by a continuous aggregate of the same
#: name by ``TimescaleManager.configure`` when the hypertable is active.
_V2 = Migration(2, "telemetry hypertable (TimescaleDB, optional) and 5-minute view", (_V2_HYPERTABLE, PLAIN_VIEW_SQL))

#: Data model of the design document (v7): directions per route, the interior map of each bus and the
#: derived capacity. Column names stay in English (project convention); ``docs/data-model-v7.md`` maps
#: every table and column to its Spanish name in the document.
#:
#: * ``routes.return_path_json``  - return trace (NULL = the outbound trace walked backwards);
#: * ``route_stops``              - one ordered list of stops PER DIRECTION, with ``progress`` (0-1) of
#:                                  the stop along the trace of that direction;
#: * ``buses.total_capacity``     - seats + standing positions + wheelchair spaces (generated);
#: * ``bus_positions``            - every seat / standing position / wheelchair space / door of a bus with
#:                                  its camera region of interest; when a bus has positions, their counts
#:                                  must match the capacity columns (checked at COMMIT, so a bus and its
#:                                  positions can be written in either order inside one transaction).
_V3_STATEMENTS = (
    "ALTER TABLE routes ADD COLUMN return_path_json TEXT",
    "ALTER TABLE route_stops DROP CONSTRAINT route_stops_pkey",
    "ALTER TABLE route_stops DROP CONSTRAINT route_stops_route_id_stop_id_key",
    "ALTER TABLE route_stops ADD COLUMN direction TEXT NOT NULL DEFAULT 'outbound' "
    "CHECK (direction IN ('outbound', 'return'))",
    "ALTER TABLE route_stops ALTER COLUMN direction DROP DEFAULT",
    "ALTER TABLE route_stops ADD COLUMN progress DOUBLE PRECISION CHECK (progress IS NULL OR progress BETWEEN 0 AND 1)",
    "ALTER TABLE route_stops ADD PRIMARY KEY (route_id, direction, stop_order)",
    "ALTER TABLE route_stops ADD CONSTRAINT uq_route_stops_stop UNIQUE (route_id, direction, stop_id)",
    "CREATE INDEX idx_route_stops_stop ON route_stops (stop_id)",
    # Existing routes had one list for both ways: the return list is that list reversed.
    "INSERT INTO route_stops (route_id, direction, stop_order, stop_id) "
    "SELECT route_id, 'return', count(*) OVER (PARTITION BY route_id) + 1 - stop_order, stop_id "
    "FROM route_stops WHERE direction = 'outbound'",
    "ALTER TABLE buses ADD COLUMN total_capacity INTEGER "
    "GENERATED ALWAYS AS (seats + standing_positions + wheelchair_spaces) STORED",
    """CREATE TABLE bus_positions (
        position_id BIGSERIAL PRIMARY KEY,
        bus_id      TEXT NOT NULL REFERENCES buses (bus_id) ON DELETE CASCADE,
        kind        TEXT NOT NULL CHECK (kind IN ('seat', 'standing', 'wheelchair', 'door')),
        row_no      INTEGER NOT NULL CHECK (row_no >= 1),
        side        TEXT NOT NULL CHECK (side IN ('left', 'right', 'aisle', 'rear')),
        camera      TEXT NOT NULL CHECK (camera IN ('front', 'rear')),
        roi         JSONB NOT NULL
    )""",
    "CREATE INDEX idx_bus_positions_bus ON bus_positions (bus_id, kind)",
    """CREATE FUNCTION check_bus_positions() RETURNS trigger LANGUAGE plpgsql AS $fn$
    DECLARE
        target  TEXT;
        seats_n INTEGER; standing_n INTEGER; chairs_n INTEGER; total_n INTEGER;
        bus     RECORD;
    BEGIN
        IF TG_TABLE_NAME = 'bus_positions' THEN
            target := COALESCE(NEW.bus_id, OLD.bus_id);
        ELSE
            target := NEW.bus_id;
        END IF;
        SELECT count(*) FILTER (WHERE kind = 'seat'), count(*) FILTER (WHERE kind = 'standing'),
               count(*) FILTER (WHERE kind = 'wheelchair'), count(*)
          INTO seats_n, standing_n, chairs_n, total_n
          FROM bus_positions WHERE bus_id = target;
        IF total_n = 0 THEN
            RETURN NULL;  -- interior not mapped yet
        END IF;
        SELECT seats, standing_positions, wheelchair_spaces INTO bus FROM buses WHERE bus_id = target;
        IF NOT FOUND THEN
            RETURN NULL;  -- the bus is being deleted (positions go with it)
        END IF;
        IF seats_n <> bus.seats OR standing_n <> bus.standing_positions OR chairs_n <> bus.wheelchair_spaces THEN
            RAISE EXCEPTION USING ERRCODE = 'check_violation',
                MESSAGE = 'bus_positions_mismatch: bus ' || target || ' has ' || seats_n || ' seats, '
                    || standing_n || ' standing and ' || chairs_n || ' wheelchair positions mapped, '
                    || 'but its capacity says ' || bus.seats || ', ' || bus.standing_positions
                    || ' and ' || bus.wheelchair_spaces;
        END IF;
        RETURN NULL;
    END
    $fn$""",
    "CREATE CONSTRAINT TRIGGER trg_bus_positions_match AFTER INSERT OR UPDATE OR DELETE ON bus_positions "
    "DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION check_bus_positions()",
    "CREATE CONSTRAINT TRIGGER trg_buses_capacity_match "
    "AFTER UPDATE OF seats, standing_positions, wheelchair_spaces ON buses "
    "DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION check_bus_positions()",
)
_V3 = Migration(3, "v7 data model: route directions, stop progress, bus positions, generated capacity", _V3_STATEMENTS)

MIGRATIONS: tuple[Migration, ...] = (_V1, _V2, _V3)

#: Any number works as long as every replica uses the same one.
_MIGRATION_LOCK_ID = 727_274_001


class Migrator:
    def __init__(self, database: Database, clock: Clock | None = None) -> None:
        self._db = database
        self._clock = clock or SystemClock()

    def _lock_and_prepare(self) -> None:
        """Serialise every replica, then make sure the bookkeeping table exists.

        The lock comes FIRST: ``CREATE TABLE IF NOT EXISTS`` is not safe when several sessions run it at
        the same moment (PostgreSQL can answer with a unique violation on its own catalogue)."""
        self._db.execute("SELECT pg_advisory_xact_lock(%s)", (_MIGRATION_LOCK_ID,))
        self._db.execute(
            "CREATE TABLE IF NOT EXISTS schema_migrations "
            "(version INTEGER PRIMARY KEY, description TEXT NOT NULL, applied_at DOUBLE PRECISION NOT NULL)"
        )

    def applied_versions(self) -> list[int]:
        with self._db.transaction():
            self._lock_and_prepare()
            rows = self._db.query_all("SELECT version FROM schema_migrations ORDER BY version")
        return [row["version"] for row in rows]

    def migrate(self) -> list[int]:
        """Apply pending migrations in order; returns the versions applied by *this* call.

        Everything happens in ONE transaction that holds the lock: a replica that starts while another
        one is migrating waits, then finds nothing left to do; a failure leaves the database untouched."""
        applied: list[int] = []
        with self._db.transaction():
            self._lock_and_prepare()
            done = {row["version"] for row in self._db.query_all("SELECT version FROM schema_migrations")}
            for migration in sorted(MIGRATIONS, key=lambda m: m.version):
                if migration.version in done:
                    continue
                for statement in migration.statements:
                    self._db.execute(statement)
                self._db.execute(
                    "INSERT INTO schema_migrations (version, description, applied_at) VALUES (%s, %s, %s)",
                    (migration.version, migration.description, self._clock.now()),
                )
                log.info("Applied migration %s: %s", migration.version, migration.description)
                applied.append(migration.version)
        return applied
