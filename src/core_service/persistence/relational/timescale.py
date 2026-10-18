"""TimescaleDB operations for the telemetry time series.

The *schema* (a hypertable) is created by migration 2. The *operating policy* lives here and is
re-applied idempotently on every start, so changing the retention in the configuration takes
effect without a new migration:

* **compression** of chunks older than ``compress_after_days`` (segmented by bus, ordered by time);
* **retention**: raw readings older than ``retention_days`` are dropped by chunk (instant, no
  ``DELETE`` bloat);
* **continuous aggregate** ``telemetry_5min``: 5-minute occupancy buckets per bus, refreshed every
  minute and queried in real time (the newest minute is computed on the fly). It is what the AI
  reads to understand trends without scanning raw rows, and it is kept for a year even after the
  raw rows are gone.

Without the extension (a developer laptop, CI) the same name ``telemetry_5min`` is a plain view
computed on demand, so the repositories and the AI tools behave identically; only the speed and
the automatic housekeeping differ. :meth:`TimescaleManager.configure` reports which mode is active.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from core_service.persistence.relational.database import Database

log = logging.getLogger(__name__)

AGGREGATE_NAME = "telemetry_5min"
AGGREGATE_RETENTION_DAYS = 365

_COLUMNS = """
    bus_id,
    count(*)                                      AS samples,
    avg(occupancy_pct)::float8                    AS avg_occupancy_pct,
    max(occupancy_pct)                            AS max_occupancy_pct,
    min(occupancy_pct)                            AS min_occupancy_pct,
    avg(confidence)::float8                       AS avg_confidence,
    count(*) FILTER (WHERE approximate_count)     AS approximate_samples
"""

PLAIN_VIEW_SQL = f"""
CREATE OR REPLACE VIEW {AGGREGATE_NAME} AS
SELECT date_bin(INTERVAL '5 minutes', ts, TIMESTAMPTZ '2000-01-01 00:00:00+00') AS bucket, {_COLUMNS}
FROM telemetry_history
GROUP BY 1, bus_id
"""  # noqa: S608 - constants only

CONTINUOUS_AGGREGATE_SQL = f"""
CREATE MATERIALIZED VIEW {AGGREGATE_NAME}
WITH (timescaledb.continuous, timescaledb.materialized_only = false) AS
SELECT time_bucket(INTERVAL '5 minutes', ts) AS bucket, {_COLUMNS}
FROM telemetry_history
GROUP BY 1, bus_id
WITH NO DATA
"""  # noqa: S608 - constants only


@dataclass(frozen=True)
class TimescaleStatus:
    """What :meth:`TimescaleManager.configure` found and did."""

    enabled: bool
    retention_days: int = 0
    compress_after_days: int = 0

    @property
    def mode(self) -> str:
        return "timescaledb" if self.enabled else "postgresql-plain"


class TimescaleManager:
    def __init__(self, database: Database) -> None:
        self._db = database

    def extension_installed(self) -> bool:
        row = self._db.query_one("SELECT 1 AS ok FROM pg_extension WHERE extname = 'timescaledb'")
        return row is not None

    def is_hypertable(self) -> bool:
        if not self.extension_installed():
            return False
        row = self._db.query_one(
            "SELECT 1 AS ok FROM timescaledb_information.hypertables WHERE hypertable_name = 'telemetry_history'"
        )
        return row is not None

    def configure(self, retention_days: int = 30, compress_after_days: int = 7) -> TimescaleStatus:
        if retention_days < 1 or compress_after_days < 1 or compress_after_days >= retention_days:
            raise ValueError("need 1 <= compress_after_days < retention_days")
        if not self.is_hypertable():
            self._ensure_plain_view()
            log.warning("TimescaleDB is not active: telemetry_history is a plain table (fine for development)")
            return TimescaleStatus(False)
        self._configure_compression(compress_after_days)
        self._configure_retention("telemetry_history", retention_days)
        self._ensure_continuous_aggregate()
        log.info("TimescaleDB configured: retention %s d, compression after %s d", retention_days, compress_after_days)
        return TimescaleStatus(True, retention_days, compress_after_days)

    # ------------------------------------------------------------------ plain mode
    def _relation_kind(self) -> str | None:
        row = self._db.query_one(
            "SELECT c.relkind AS kind FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
            "WHERE c.relname = %s AND n.nspname = ANY (current_schemas(false))",
            (AGGREGATE_NAME,),
        )
        return None if row is None else str(row["kind"])

    def _is_continuous_aggregate(self) -> bool:
        row = self._db.query_one(
            "SELECT 1 AS ok FROM timescaledb_information.continuous_aggregates "
            "WHERE view_name = %s AND view_schema = ANY (current_schemas(false))",
            (AGGREGATE_NAME,),
        )
        return row is not None

    def _ensure_plain_view(self) -> None:
        if self._relation_kind() in (None, "v"):
            self._db.execute(PLAIN_VIEW_SQL)

    # ----------------------------------------------------------- hypertable mode
    def _configure_compression(self, compress_after_days: int) -> None:
        self._db.execute(
            "ALTER TABLE telemetry_history SET (timescaledb.compress, "
            "timescaledb.compress_segmentby = 'bus_id', timescaledb.compress_orderby = 'ts DESC')"
        )
        self._db.execute("SELECT remove_compression_policy('telemetry_history', if_exists => true)")
        self._db.execute(
            "SELECT add_compression_policy('telemetry_history', make_interval(days => %s))", (compress_after_days,)
        )

    def _configure_retention(self, relation: str, days: int) -> None:
        self._db.execute("SELECT remove_retention_policy(%s::regclass, if_exists => true)", (relation,))
        self._db.execute("SELECT add_retention_policy(%s::regclass, make_interval(days => %s))", (relation, days))

    def _ensure_continuous_aggregate(self) -> None:
        # A continuous aggregate is stored as a *view* in pg_class (relkind 'v'), so the relation kind alone cannot
        # tell it from the plain view left by a run without the extension: ask TimescaleDB.
        exists = self._is_continuous_aggregate()
        if not exists and self._relation_kind() == "v":  # a plain view left by a run without the extension
            self._db.execute(f"DROP VIEW {AGGREGATE_NAME}")
        if not exists:
            self._db.execute(CONTINUOUS_AGGREGATE_SQL)  # must run outside a transaction block
        self._db.execute(
            "SELECT add_continuous_aggregate_policy(%s::regclass, start_offset => INTERVAL '1 hour', "
            "end_offset => INTERVAL '1 minute', schedule_interval => INTERVAL '1 minute', if_not_exists => true)",
            (AGGREGATE_NAME,),
        )
        self._configure_retention(AGGREGATE_NAME, AGGREGATE_RETENTION_DAYS)
