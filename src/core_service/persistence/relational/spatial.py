"""PostGIS support: geometry columns derived from the plain latitude/longitude data.

The relational tables keep ``lat``/``lng`` (and the trace as a JSON list) as the source of truth, so
the service works on any PostgreSQL. When the PostGIS extension is available this manager adds the
geometry columns that the design document (v7) describes, and keeps them in sync *inside the database*:

* ``stops.location``               ``geometry(Point, 4326)``, a generated column of ``lng``/``lat``;
* ``routes.path_outbound`` / ``routes.path_return``  ``geometry(LineString, 4326)``, filled by a trigger
  from the trace columns (the return trace is the outbound one reversed unless the route has its own);
* GiST indexes on all of them (nearby-stop queries and map matching);
* view ``telemetry_geo``           readings with a ``location`` point (a view, so it does not interfere
  with TimescaleDB compression of ``telemetry_history``).

Everything is idempotent and runs in one transaction under an advisory lock, so several replicas can
start at the same time. Without PostGIS nothing is created and :meth:`configure` says so.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import psycopg

from core_service.models import Direction, GeoPoint
from core_service.persistence.relational.database import Database, Row

log = logging.getLogger(__name__)

_SPATIAL_LOCK_ID = 727_274_002
SRID = 4326

_TRACE_SQL = """
CASE WHEN json_array_length({column}::json) >= 2 THEN ST_SetSRID(ST_MakeLine(ARRAY(
        SELECT ST_MakePoint((point ->> 0)::float8, (point ->> 1)::float8)
        FROM json_array_elements({column}::json) WITH ORDINALITY AS t(point, idx) ORDER BY idx)), 4326) END
"""

_TRIGGER_FUNCTION = f"""
CREATE OR REPLACE FUNCTION routes_sync_geometry() RETURNS trigger LANGUAGE plpgsql AS $fn$
BEGIN
    NEW.path_outbound := {_TRACE_SQL.format(column="NEW.path_json")};
    IF NEW.return_path_json IS NULL THEN
        NEW.path_return := ST_Reverse(NEW.path_outbound);
    ELSE
        NEW.path_return := {_TRACE_SQL.format(column="NEW.return_path_json")};
    END IF;
    RETURN NEW;
END
$fn$
"""

_STATEMENTS = (
    "ALTER TABLE stops ADD COLUMN IF NOT EXISTS location geometry(Point, 4326) "
    "GENERATED ALWAYS AS (ST_SetSRID(ST_MakePoint(lng, lat), 4326)) STORED",
    "ALTER TABLE routes ADD COLUMN IF NOT EXISTS path_outbound geometry(LineString, 4326)",
    "ALTER TABLE routes ADD COLUMN IF NOT EXISTS path_return geometry(LineString, 4326)",
    _TRIGGER_FUNCTION,
    "DROP TRIGGER IF EXISTS trg_routes_geometry ON routes",
    "CREATE TRIGGER trg_routes_geometry BEFORE INSERT OR UPDATE OF path_json, return_path_json ON routes "
    "FOR EACH ROW EXECUTE FUNCTION routes_sync_geometry()",
    "UPDATE routes SET path_json = path_json",  # fills the geometry of routes that already exist
    "CREATE INDEX IF NOT EXISTS idx_stops_location ON stops USING gist (location)",
    "CREATE INDEX IF NOT EXISTS idx_routes_path_outbound ON routes USING gist (path_outbound)",
    "CREATE INDEX IF NOT EXISTS idx_routes_path_return ON routes USING gist (path_return)",
    "CREATE OR REPLACE VIEW telemetry_geo AS "
    "SELECT t.*, ST_SetSRID(ST_MakePoint(t.lng, t.lat), 4326) AS location FROM telemetry_history t",
)


@dataclass(frozen=True)
class SpatialStatus:
    enabled: bool

    @property
    def mode(self) -> str:
        return "postgis" if self.enabled else "none"


class SpatialManager:
    def __init__(self, database: Database) -> None:
        self._db = database

    def extension_installed(self) -> bool:
        return self._db.query_one("SELECT 1 AS ok FROM pg_extension WHERE extname = 'postgis'") is not None

    def _ensure_extension(self) -> bool:
        if self.extension_installed():
            return True
        available = self._db.query_one("SELECT 1 AS ok FROM pg_available_extensions WHERE name = 'postgis'")
        if available is None:
            return False
        try:
            self._db.execute("CREATE EXTENSION IF NOT EXISTS postgis SCHEMA public")
        except psycopg.Error as error:  # PostGIS is not a trusted extension: a DBA may have to create it
            log.warning("PostGIS is installed on the server but could not be enabled: %s", type(error).__name__)
            return False
        return True

    def configure(self) -> SpatialStatus:
        if not self._ensure_extension():
            log.warning("PostGIS is not active: geometry columns are not created (lat/lng columns still work)")
            return SpatialStatus(False)
        with self._db.transaction():
            self._db.execute("SELECT pg_advisory_xact_lock(%s)", (_SPATIAL_LOCK_ID,))
            for statement in _STATEMENTS:
                self._db.execute(statement)
        log.info("PostGIS geometry columns are in place")
        return SpatialStatus(True)

    # --------------------------------------------------------------------- queries
    def stops_near(self, point: GeoPoint, radius_m: float, limit: int = 10) -> list[Row]:
        """Stops within ``radius_m`` metres of ``point``, nearest first (uses the GiST index)."""
        return self._db.query_all(
            "SELECT stop_id, name, "
            "ST_Distance(location::geography, ST_SetSRID(ST_MakePoint(%s, %s), 4326)::geography) AS distance_m "
            "FROM stops WHERE active AND ST_DWithin(location::geography, "
            "ST_SetSRID(ST_MakePoint(%s, %s), 4326)::geography, %s) ORDER BY distance_m, stop_id LIMIT %s",
            (point.lng, point.lat, point.lng, point.lat, radius_m, max(1, limit)),
        )

    def progress_mismatches(self, tolerance: float = 0.01) -> list[Row]:
        """Stops whose stored ``progress`` differs from PostGIS' ``ST_LineLocatePoint`` by more than
        ``tolerance``. A quality check for the catalogue, not something the service depends on."""
        return self._db.query_all(
            "SELECT * FROM ("
            "  SELECT rs.route_id, rs.direction, rs.stop_id, rs.progress AS stored, "
            "         ST_LineLocatePoint(CASE rs.direction WHEN %s THEN r.path_outbound ELSE r.path_return END, "
            "                            s.location) AS postgis "
            "  FROM route_stops rs JOIN routes r ON r.route_id = rs.route_id JOIN stops s ON s.stop_id = rs.stop_id "
            "  WHERE rs.progress IS NOT NULL) q WHERE abs(stored - postgis) > %s ORDER BY route_id, direction, stop_id",
            (Direction.OUTBOUND.value, tolerance),
        )
