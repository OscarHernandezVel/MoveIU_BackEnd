"""Data model of the design document (v7): route directions, stop progress, bus positions, PostGIS."""

from __future__ import annotations

from typing import Any

import psycopg
import pytest

from core_service.clock import FakeClock
from core_service.models import BusCapacity, BusPosition, Direction, GeoPoint, PositionKind, Stop
from core_service.occupancy.layout import default_layout
from core_service.persistence.relational import (
    MIGRATIONS,
    CatalogRepository,
    Database,
    Migrator,
    SpatialManager,
    SpatialStatus,
)
from core_service.persistence.relational import migrations as migrations_module
from factories import BUS_41, BUS_ACCESSIBLE, MINIBUS_25, demo_buses, demo_data, demo_stops
from pg import make_database


@pytest.fixture
def db() -> Database:
    return make_database(FakeClock())


@pytest.fixture
def repo(db: Database) -> CatalogRepository:
    repository = CatalogRepository(db)
    for stop in demo_stops().values():
        repository.save_stop(stop)
    route = demo_data()["routes"][0]
    repository.save_route(
        route["route_id"], route["name"], [GeoPoint(lat, lng) for lng, lat in route["path"]], route["stops"]
    )
    for bus in demo_buses():
        repository.save_bus(bus)
    return repository


def counts(positions: list[BusPosition]) -> dict[str, int]:
    result: dict[str, int] = {}
    for position in positions:
        result[position.kind.value] = result.get(position.kind.value, 0) + 1
    return result


class TestDefaultLayout:
    @pytest.mark.parametrize("capacity", [BUS_41, BUS_ACCESSIBLE, MINIBUS_25, BusCapacity(30, 10, 0, 3)])
    def test_the_map_has_one_position_per_unit_of_capacity(self, capacity: BusCapacity) -> None:
        found = counts(default_layout(capacity))
        assert found.get("seat", 0) == capacity.seats
        assert found.get("standing", 0) == capacity.standing_positions
        assert found.get("wheelchair", 0) == capacity.wheelchair_spaces
        assert found.get("door", 0) == capacity.door_zones

    def test_doors_do_not_add_capacity(self) -> None:
        with_doors = default_layout(BusCapacity(41, 8, 0, 4))
        assert sum(p.kind is not PositionKind.DOOR for p in with_doors) == 49

    def test_a_standing_position_needs_a_row_with_seats_on_both_sides(self) -> None:
        with pytest.raises(ValueError, match="do not fit"):
            default_layout(BusCapacity(seats=12, standing_positions=9))

    def test_every_generated_position_is_marked_as_not_calibrated(self) -> None:
        layout = default_layout(BUS_41)
        assert all(not p.calibrated and p.roi["polygon"] == [] for p in layout)

    def test_a_calibrated_roi_needs_a_real_polygon(self) -> None:
        from core_service.errors import ValidationError
        from core_service.models import Camera, PositionSide

        with pytest.raises(ValidationError):
            BusPosition(
                PositionKind.SEAT, 1, PositionSide.LEFT, Camera.FRONT, {"calibrated": True, "polygon": [[0, 0]]}
            )

    def test_the_last_row_is_the_rear_row_of_five_seats(self) -> None:
        layout = default_layout(BUS_41)
        rear = [p for p in layout if p.side.value == "rear"]
        assert len(rear) == 5 and len({p.row_no for p in rear}) == 1


class TestSchemaV3:
    def test_capacity_is_generated_from_seats_standing_and_wheelchair(
        self, db: Database, repo: CatalogRepository
    ) -> None:
        rows = {r["bus_id"]: r["total_capacity"] for r in db.query_all("SELECT bus_id, total_capacity FROM buses")}
        assert rows == {"1045": 49, "1102": 50, "1088": 29}

    def test_the_generated_capacity_cannot_be_written(self, db: Database, repo: CatalogRepository) -> None:
        with pytest.raises(psycopg.errors.GeneratedAlways):
            db.execute("UPDATE buses SET total_capacity = 1 WHERE bus_id = '1045'")

    def test_each_direction_has_its_own_ordered_stop_list_and_the_default_return_is_the_reverse(
        self, db: Database, repo: CatalogRepository
    ) -> None:
        out = [
            r["stop_id"]
            for r in db.query_all(
                "SELECT stop_id FROM route_stops WHERE route_id='C5' AND direction='outbound' ORDER BY stop_order"
            )
        ]
        back = [
            r["stop_id"]
            for r in db.query_all(
                "SELECT stop_id FROM route_stops WHERE route_id='C5' AND direction='return' ORDER BY stop_order"
            )
        ]
        assert back == list(reversed(out)) and len(out) == 6

    def test_a_route_can_have_its_own_return_stops_and_trace(self, db: Database, repo: CatalogRepository) -> None:
        route = demo_data()["routes"][0]
        path = [GeoPoint(lat, lng) for lng, lat in route["path"]]
        back_path = [GeoPoint(p.lat + 0.0002, p.lng) for p in reversed(path)]
        repo.save_route(
            "C5",
            route["name"],
            path,
            route["stops"],
            return_path=back_path,
            return_stop_ids=["P-031", "P-018", "P-001"],
        )
        loaded = repo.load_routes()[0]
        assert [s.stop_id for s in loaded.return_stops] == ["P-031", "P-018", "P-001"]
        assert loaded.return_path is not None and len(loaded.return_path) == len(path)
        assert not loaded.return_is_mirror

    def test_the_default_return_is_recognised_as_a_mirror(self, repo: CatalogRepository) -> None:
        assert repo.load_routes()[0].return_is_mirror

    def test_a_stop_cannot_appear_twice_in_the_same_direction(self, db: Database, repo: CatalogRepository) -> None:
        with pytest.raises(psycopg.errors.UniqueViolation):
            db.execute(
                "INSERT INTO route_stops (route_id, direction, stop_order, stop_id) VALUES ('C5','outbound',99,'P-001')"
            )

    def test_the_direction_is_checked(self, db: Database, repo: CatalogRepository) -> None:
        with pytest.raises(psycopg.errors.CheckViolation):
            db.execute(
                "INSERT INTO route_stops (route_id, direction, stop_order, stop_id) VALUES ('C5','sideways',99,'P-009')"
            )

    def test_progress_is_stored_per_stop_and_direction(self, repo: CatalogRepository) -> None:
        repo.set_stop_progress("C5", Direction.OUTBOUND, {"P-001": 0.0, "P-031": 0.9})
        stored = repo.stop_progress("C5", Direction.OUTBOUND)
        assert stored["P-001"] == 0.0 and stored["P-031"] == 0.9 and stored["P-009"] is None

    def test_progress_out_of_range_is_clamped_to_the_valid_interval(self, repo: CatalogRepository) -> None:
        repo.set_stop_progress("C5", Direction.OUTBOUND, {"P-001": 1.7})
        assert repo.stop_progress("C5", Direction.OUTBOUND)["P-001"] == 1.0

    def test_the_migration_upgrades_existing_data(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A database on version 2 (one stop list per route) gets both directions and keeps its rows."""
        database = make_database(migrate=False)
        monkeypatch.setattr(migrations_module, "MIGRATIONS", MIGRATIONS[:2])
        Migrator(database, FakeClock()).migrate()
        route = demo_data()["routes"][0]
        for stop in demo_stops().values():
            database.execute(
                "INSERT INTO stops (stop_id, name, lat, lng) VALUES (%s, %s, %s, %s)",
                (stop.stop_id, stop.name, stop.location.lat, stop.location.lng),
            )
        database.execute("INSERT INTO routes (route_id, name, path_json) VALUES ('C5', 'x', '[[0,0],[1,1]]')")
        for order, stop_id in enumerate(route["stops"], start=1):
            database.execute(
                "INSERT INTO route_stops (route_id, stop_order, stop_id) VALUES ('C5', %s, %s)", (order, stop_id)
            )
        monkeypatch.setattr(migrations_module, "MIGRATIONS", MIGRATIONS)
        assert Migrator(database, FakeClock()).migrate() == [3]
        out = [
            r["stop_id"]
            for r in database.query_all(
                "SELECT stop_id FROM route_stops WHERE direction='outbound' ORDER BY stop_order"
            )
        ]
        back = [
            r["stop_id"]
            for r in database.query_all("SELECT stop_id FROM route_stops WHERE direction='return' ORDER BY stop_order")
        ]
        assert out == route["stops"] and back == list(reversed(route["stops"]))
        row = database.query_one("SELECT return_path_json FROM routes WHERE route_id='C5'")
        assert row is not None and row["return_path_json"] is None


class TestBusPositions:
    def test_positions_round_trip_with_their_roi(self, repo: CatalogRepository) -> None:
        layout = default_layout(BUS_41)
        repo.save_bus_positions("1045", layout)
        loaded = repo.load_bus_positions("1045")
        assert counts(loaded) == counts(layout) and loaded[0].roi == {"calibrated": False, "polygon": []}

    def test_a_calibrated_polygon_is_stored_as_json(self, db: Database, repo: CatalogRepository) -> None:
        from core_service.models import Camera, PositionSide

        polygon = [[10, 10], [50, 10], [50, 40], [10, 40]]
        layout = default_layout(BUS_41)
        layout[0] = BusPosition(
            PositionKind.SEAT, 1, PositionSide.LEFT, Camera.FRONT, {"calibrated": True, "polygon": polygon}
        )
        repo.save_bus_positions("1045", layout)
        row = db.query_one("SELECT roi FROM bus_positions WHERE bus_id='1045' ORDER BY position_id LIMIT 1")
        assert row is not None and row["roi"]["polygon"] == polygon

    def test_saving_again_replaces_the_previous_map(self, db: Database, repo: CatalogRepository) -> None:
        repo.save_bus_positions("1045", default_layout(BUS_41))
        repo.save_bus_positions("1045", default_layout(BUS_41))
        assert db.query_one("SELECT count(*) AS n FROM bus_positions WHERE bus_id='1045'") == {"n": 51}

    def test_counts_that_do_not_match_the_capacity_are_rejected_at_commit(self, repo: CatalogRepository) -> None:
        wrong = default_layout(BusCapacity(40, 8, 0, 2))  # the bus has 41 seats
        with pytest.raises(psycopg.errors.CheckViolation, match="bus_positions_mismatch"):
            repo.save_bus_positions("1045", wrong)

    def test_a_bus_without_a_map_is_allowed(self, db: Database, repo: CatalogRepository) -> None:
        assert db.query_one("SELECT count(*) AS n FROM bus_positions") == {"n": 0}

    def test_changing_the_capacity_of_a_mapped_bus_is_rejected(self, db: Database, repo: CatalogRepository) -> None:
        repo.save_bus_positions("1045", default_layout(BUS_41))
        with pytest.raises(psycopg.errors.CheckViolation, match="bus_positions_mismatch"):
            db.execute("UPDATE buses SET seats = 42 WHERE bus_id = '1045'")

    def test_the_capacity_and_the_map_can_change_together_in_one_transaction(
        self, db: Database, repo: CatalogRepository
    ) -> None:
        repo.save_bus_positions("1045", default_layout(BUS_41))
        with db.transaction():
            db.execute("UPDATE buses SET seats = 42 WHERE bus_id = '1045'")
            repo.save_bus_positions("1045", default_layout(BusCapacity(42, 8, 0, 2)))
        assert db.query_one("SELECT count(*) AS n FROM bus_positions WHERE bus_id='1045' AND kind='seat'") == {"n": 42}

    def test_deleting_a_bus_deletes_its_map(self, db: Database, repo: CatalogRepository) -> None:
        repo.save_bus_positions("1045", default_layout(BUS_41))
        db.execute("DELETE FROM buses WHERE bus_id = '1045'")
        assert db.query_one("SELECT count(*) AS n FROM bus_positions") == {"n": 0}

    def test_unknown_kinds_and_sides_are_refused_by_the_database(self, db: Database, repo: CatalogRepository) -> None:
        with pytest.raises(psycopg.errors.CheckViolation):
            db.execute(
                "INSERT INTO bus_positions (bus_id, kind, row_no, side, camera, roi) "
                "VALUES ('1045', 'roof', 1, 'left', 'front', '{}')"
            )


# ---------------------------------------------------------------------------------- PostGIS
class _NoPostgis:
    """Database double for a server without the extension: records what would be executed."""

    def __init__(self) -> None:
        self.executed: list[str] = []

    def query_one(self, sql: str, params: Any = ()) -> None:
        return None

    def execute(self, sql: str, params: Any = ()) -> int:
        self.executed.append(sql)
        return 0


class TestSpatialWithoutPostgis:
    def test_nothing_is_created_and_the_status_says_so(self) -> None:
        double = _NoPostgis()
        status = SpatialManager(double).configure()  # type: ignore[arg-type]
        assert status == SpatialStatus(False) and status.mode == "none" and double.executed == []


def _has_postgis(db: Database) -> bool:
    return db.query_one("SELECT 1 AS ok FROM pg_available_extensions WHERE name = 'postgis'") is not None


class TestSpatialWithPostgis:
    @pytest.fixture
    def spatial(self, db: Database, repo: CatalogRepository) -> SpatialManager:
        if not _has_postgis(db):
            pytest.skip("PostGIS no está instalado en este servidor")
        manager = SpatialManager(db)
        assert manager.configure().enabled
        return manager

    def test_configuring_twice_is_harmless(self, spatial: SpatialManager) -> None:
        assert spatial.configure() == SpatialStatus(True)

    def test_stops_get_a_generated_point(self, db: Database, spatial: SpatialManager) -> None:
        row = db.query_one(
            "SELECT ST_X(location) AS x, ST_Y(location) AS y, ST_SRID(location) AS srid FROM stops WHERE stop_id='P-018'"
        )
        assert row is not None and row["srid"] == 4326
        assert (round(row["x"], 4), round(row["y"], 4)) == (-77.2811, 1.2136)

    def test_the_trace_columns_are_filled_by_the_trigger_for_existing_and_new_routes(
        self, db: Database, repo: CatalogRepository, spatial: SpatialManager
    ) -> None:
        row = db.query_one(
            "SELECT ST_NPoints(path_outbound) AS n, ST_NPoints(path_return) AS m FROM routes WHERE route_id='C5'"
        )
        assert row == {"n": 7, "m": 7}
        route = demo_data()["routes"][0]
        path = [GeoPoint(lat, lng) for lng, lat in route["path"]]
        repo.save_route("C6", "Nueva", path[:3], ["P-001", "P-009"], return_path=path[3:6])
        row = db.query_one(
            "SELECT ST_NPoints(path_outbound) AS n, ST_NPoints(path_return) AS m FROM routes WHERE route_id='C6'"
        )
        assert row == {"n": 3, "m": 3}

    def test_without_a_return_trace_it_is_the_outbound_one_reversed(
        self, db: Database, spatial: SpatialManager
    ) -> None:
        row = db.query_one(
            "SELECT ST_Equals(ST_Reverse(path_outbound), path_return) AS same, ST_Length(path_outbound::geography) AS m "
            "FROM routes WHERE route_id='C5'"
        )
        assert row is not None and row["same"] is True and row["m"] > 1000

    def test_updating_the_trace_updates_the_geometry(self, db: Database, spatial: SpatialManager) -> None:
        db.execute("UPDATE routes SET path_json = '[[-77.3,1.2],[-77.2,1.2]]' WHERE route_id = 'C5'")
        row = db.query_one("SELECT ST_NPoints(path_outbound) AS n FROM routes WHERE route_id='C5'")
        assert row == {"n": 2}

    def test_nearby_stops_are_found_nearest_first(self, spatial: SpatialManager) -> None:
        found = spatial.stops_near(GeoPoint(1.2136, -77.2811), 1_000, 3)
        assert found[0]["stop_id"] == "P-018" and found[0]["distance_m"] < 1
        assert [r["distance_m"] for r in found] == sorted(r["distance_m"] for r in found)
        assert spatial.stops_near(GeoPoint(0.0, 0.0), 500) == []

    def test_postgis_agrees_with_the_progress_computed_by_the_service(
        self, repo: CatalogRepository, spatial: SpatialManager
    ) -> None:
        from core_service.metrics import MetricsRegistry
        from core_service.routing import RouteCatalog
        from core_service.services.catalog_service import CatalogService

        CatalogService(repo, RouteCatalog(), MetricsRegistry()).reload()  # stores the progress of every stop
        assert spatial.progress_mismatches(0.005) == []
        repo.set_stop_progress("C5", Direction.OUTBOUND, {"P-018": 0.99})  # a wrong value is caught
        assert [r["stop_id"] for r in spatial.progress_mismatches(0.005)] == ["P-018"]

    def test_the_telemetry_view_exposes_a_point(self, db: Database, spatial: SpatialManager) -> None:
        from core_service.persistence.relational import TelemetryRepository
        from factories import make_reading

        TelemetryRepository(db).save(make_reading(), 50)
        row = db.query_one("SELECT ST_Y(location) AS lat FROM telemetry_geo")
        assert row is not None and round(row["lat"], 4) == 1.2136


def test_the_stop_model_is_unchanged_by_the_new_columns(repo: CatalogRepository) -> None:
    assert repo.load_routes()[0].stops[0] == Stop("P-001", "Terminal Sur", GeoPoint(1.198, -77.29))
