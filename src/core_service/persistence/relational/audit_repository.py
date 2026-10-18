"""Audit samples and telemetry history."""

from __future__ import annotations

from typing import Any

from core_service.models import AuditSample, Reading
from core_service.persistence.relational.database import Database, Row


def _sample(row: Row) -> AuditSample:
    return AuditSample(
        sample_id=row["sample_id"],
        bus_id=row["bus_id"],
        stop_id=row["stop_id"],
        auditor_id=row["auditor_id"],
        manual_seated=row["manual_seated"],
        manual_standing=row["manual_standing"],
        model_seated=row["model_seated"],
        model_standing=row["model_standing"],
        doors_total=row["doors_total"],
        confidence=row["confidence"],
        model_version=row["model_version"],
        sampled_at=row["sampled_at"],
    )


class AuditRepository:
    def __init__(self, database: Database) -> None:
        self._db = database

    def add(self, sample: AuditSample) -> None:
        self._db.execute(
            "INSERT INTO audit_samples (sample_id, bus_id, stop_id, auditor_id, manual_seated, manual_standing, "
            "model_seated, model_standing, doors_total, confidence, model_version, sampled_at) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
            (
                sample.sample_id,
                sample.bus_id,
                sample.stop_id,
                sample.auditor_id,
                sample.manual_seated,
                sample.manual_standing,
                sample.model_seated,
                sample.model_standing,
                sample.doors_total,
                sample.confidence,
                sample.model_version,
                sample.sampled_at,
            ),
        )

    def list_between(
        self, start: float, end: float, route_id: str | None = None
    ) -> list[tuple[AuditSample, str | None]]:
        """Samples in ``[start, end)`` with the route of their bus."""
        rows = self._db.query_all(
            "SELECT a.*, b.route_id AS bus_route FROM audit_samples a JOIN buses b ON b.bus_id = a.bus_id "
            "WHERE a.sampled_at >= %s AND a.sampled_at < %s AND (%s::text IS NULL OR b.route_id = %s) "
            "ORDER BY a.sampled_at",
            (start, end, route_id, route_id),
        )
        return [(_sample(r), r["bus_route"]) for r in rows]


_TELEMETRY_COLUMNS = (
    "bus_id, EXTRACT(EPOCH FROM ts)::float8 AS ts, lat, lng, seated, standing, standing_out_of_position, "
    "doors_total, confidence, approximate_count, occupancy_pct, model_version"
)


class TelemetryRepository:
    """Time series of readings (a TimescaleDB hypertable in production).

    ``ts`` is stored as ``TIMESTAMPTZ`` but always enters and leaves this class as epoch seconds."""

    def __init__(self, database: Database) -> None:
        self._db = database

    def save(self, reading: Reading, occupancy_pct: int) -> bool:
        """Idempotent insert: replays (offline bursts, retries) do not duplicate rows."""
        return (
            self._db.execute(
                "INSERT INTO telemetry_history (bus_id, ts, lat, lng, seated, standing, "
                "standing_out_of_position, doors_total, confidence, approximate_count, occupancy_pct, model_version) "
                "VALUES (%s, to_timestamp(%s), %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) "
                "ON CONFLICT (bus_id, ts) DO NOTHING",
                (
                    reading.bus_id,
                    reading.ts,
                    reading.location.lat,
                    reading.location.lng,
                    reading.seated,
                    reading.standing,
                    reading.standing_out_of_position,
                    reading.doors_total,
                    reading.confidence,
                    reading.approximate_count,
                    occupancy_pct,
                    reading.model_version,
                ),
            )
            > 0
        )

    def nearest(self, bus_id: str, ts: float, tolerance_seconds: float = 10.0) -> dict[str, Any] | None:
        return self._db.query_one(
            f"SELECT {_TELEMETRY_COLUMNS} FROM telemetry_history "  # noqa: S608 - constant column list
            "WHERE bus_id = %s AND ts BETWEEN to_timestamp(%s) AND to_timestamp(%s) "
            "ORDER BY ABS(EXTRACT(EPOCH FROM ts) - %s) LIMIT 1",
            (bus_id, ts - tolerance_seconds, ts + tolerance_seconds, ts),
        )

    def recent(self, bus_id: str, limit: int = 20) -> list[dict[str, Any]]:
        return self._db.query_all(
            f"SELECT {_TELEMETRY_COLUMNS} FROM telemetry_history "  # noqa: S608 - constant column list
            "WHERE bus_id = %s ORDER BY ts DESC LIMIT %s",
            (bus_id, limit),
        )

    def count(self) -> int:
        row = self._db.query_one("SELECT COUNT(*) AS n FROM telemetry_history")
        return 0 if row is None else int(row["n"])

    def prune_older_than(self, ts: float) -> int:
        """Retention for plain PostgreSQL. With TimescaleDB the retention policy drops whole chunks
        and this is only a safety net (it finds nothing to delete)."""
        return self._db.execute("DELETE FROM telemetry_history WHERE ts < to_timestamp(%s)", (ts,))

    def recent_occupancy_many(self, bus_ids: list[str], per_bus: int, since: float) -> dict[str, list[int]]:
        """Last ``per_bus`` occupancy percentages of every bus (newest first) in ONE query.

        ``since`` bounds the scan to recent chunks of the hypertable, so the cost does not grow with the
        history; the window function replaces one round trip per bus."""
        if not bus_ids:
            return {}
        rows = self._db.query_all(
            "SELECT bus_id, occupancy_pct FROM ("
            "  SELECT bus_id, occupancy_pct, ts, row_number() OVER (PARTITION BY bus_id ORDER BY ts DESC) AS rn "
            "  FROM telemetry_history WHERE bus_id = ANY(%s) AND ts >= to_timestamp(%s)"
            ") recent WHERE rn <= %s ORDER BY bus_id, ts DESC",
            (bus_ids, since, per_bus),
        )
        result: dict[str, list[int]] = {}
        for row in rows:
            result.setdefault(row["bus_id"], []).append(int(row["occupancy_pct"]))
        return result

    # ---------------------------------------------------- aggregates for analysis
    def bus_trend(self, bus_id: str, since: float, limit: int = 36) -> list[dict[str, Any]]:
        """5-minute buckets of one bus since ``since``, newest first (continuous aggregate)."""
        return self._db.query_all(
            "SELECT EXTRACT(EPOCH FROM bucket)::float8 AS bucket_ts, samples, avg_occupancy_pct, max_occupancy_pct, "
            "min_occupancy_pct, avg_confidence, approximate_samples FROM telemetry_5min "
            "WHERE bus_id = %s AND bucket >= to_timestamp(%s) ORDER BY bucket DESC LIMIT %s",
            (bus_id, since, limit),
        )

    def route_trend(self, route_id: str, since: float, limit: int = 36) -> list[dict[str, Any]]:
        """5-minute occupancy of a whole route (all its buses together), newest first."""
        return self._db.query_all(
            "SELECT EXTRACT(EPOCH FROM t.bucket)::float8 AS bucket_ts, sum(t.samples)::int AS samples, "
            "(sum(t.avg_occupancy_pct * t.samples) / NULLIF(sum(t.samples), 0))::float8 AS avg_occupancy_pct, "
            "max(t.max_occupancy_pct) AS max_occupancy_pct, count(DISTINCT t.bus_id)::int AS buses "
            "FROM telemetry_5min t JOIN buses b ON b.bus_id = t.bus_id "
            "WHERE b.route_id = %s AND t.bucket >= to_timestamp(%s) "
            "GROUP BY t.bucket ORDER BY t.bucket DESC LIMIT %s",
            (route_id, since, limit),
        )
