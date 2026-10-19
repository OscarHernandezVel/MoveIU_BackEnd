"""Catalogue ingestion: package validation, the public-data adapter, the loader and the command line."""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import pytest
from psycopg import sql

from core_service.clock import FakeClock
from core_service.ingestion import (
    CatalogLoader,
    CatalogPackageError,
    load_avante_routes,
    load_package,
    parse_package,
)
from core_service.ingestion.__main__ import main as cli_main
from core_service.ingestion.avante import parse_rutas_js
from core_service.metrics import MetricsRegistry
from core_service.models import BusCapacity, Direction, GeoPoint
from core_service.occupancy.layout import default_layout
from core_service.persistence.relational import CatalogRepository, Database
from core_service.routing import RouteCatalog
from core_service.services.catalog_service import CatalogService
from pg import database_settings, make_database

ROOT = Path(__file__).resolve().parents[2]
REAL_PACKAGE = ROOT / "demo_data" / "pasto_c1_c4.json"
FLEET = ROOT / "demo_data" / "fleet_pasto.json"


# ------------------------------------------------------------------------------- builders
def straight_path(points: int = 11, lat: float = 1.2, start: float = -77.30, end: float = -77.28) -> list[list[float]]:
    step = (end - start) / (points - 1)
    return [[round(start + i * step, 6), lat] for i in range(points)]


_COUNT_QUERIES = {
    "stops": "SELECT count(*) AS n FROM stops",
    "routes": "SELECT count(*) AS n FROM routes",
    "route_stops": "SELECT count(*) AS n FROM route_stops",
    "buses": "SELECT count(*) AS n FROM buses",
    "bus_positions": "SELECT count(*) AS n FROM bus_positions",
}


def _row_counts(db):
    return [db.query_one(query) for query in _COUNT_QUERIES.values()]


def stop_at(stop_id: str, lng: float, lat: float = 1.2, name: str | None = None) -> dict[str, Any]:
    return {"stop_id": stop_id, "name": name or f"Parada {stop_id}", "lat": lat, "lng": lng}


def bus(bus_id: str, route_id: str | None = "T1", **overrides: Any) -> dict[str, Any]:
    item = {
        "bus_id": bus_id,
        "route_id": route_id,
        "model": "Bus 12 m",
        "seats": 41,
        "standing_positions": 8,
        "wheelchair_spaces": 0,
        "door_zones": 2,
        "status": "active",
    }
    item.update(overrides)
    return item


def package_data() -> dict[str, Any]:
    stops = [stop_at("S1", -77.298), stop_at("S2", -77.294), stop_at("S3", -77.290), stop_at("S4", -77.286)]
    return {
        "format": "core-catalog/1",
        "provenance": {"source": "test"},
        "stops": stops,
        "routes": [{"route_id": "T1", "name": "Prueba", "path": straight_path(), "stops": ["S1", "S2", "S3", "S4"]}],
        "buses": [bus("9001"), bus("9002", seats=25, standing_positions=4)],
    }


@pytest.fixture
def db() -> Database:
    return make_database(FakeClock())


@pytest.fixture
def repo(db: Database) -> CatalogRepository:
    return CatalogRepository(db)


@pytest.fixture
def loader(db: Database, repo: CatalogRepository) -> CatalogLoader:
    return CatalogLoader(db, repo)


# -------------------------------------------------------------------------------- package
class TestPackageValidation:
    def test_a_valid_package_is_accepted_and_round_trips(self) -> None:
        package = parse_package(package_data())
        again = parse_package(json.loads(package.dumps()))
        assert [r.route_id for r in again.routes] == ["T1"] and set(again.stops) == set(package.stops)
        assert [b.bus.bus_id for b in again.buses] == ["9001", "9002"]

    def test_the_format_marker_is_optional_but_a_different_one_is_refused(self) -> None:
        data = package_data()
        del data["format"]
        parse_package(data)
        data["format"] = "other/9"
        with pytest.raises(CatalogPackageError, match="unsupported format"):
            parse_package(data)

    @pytest.mark.parametrize("payload", ["[]", "42", "not json", "{"])
    def test_garbage_is_refused_with_a_clear_message(self, payload: str) -> None:
        with pytest.raises(CatalogPackageError):
            load_package(payload)

    @pytest.mark.parametrize(
        ("mutate", "expected"),
        [
            (lambda d: d["stops"][0].update(lat=95), "lat/lng out of range"),
            (lambda d: d["stops"][0].update(lng="x"), "lat/lng out of range"),
            (lambda d: d["stops"][0].update(stop_id="bad id!"), "stop_id must match"),
            (lambda d: d["stops"].append(dict(d["stops"][0])), "is repeated"),
            (lambda d: d["stops"][0].update(name=" "), "name must be"),
            (lambda d: d["routes"][0].update(route_id="way-too-long-id"), "route_id must match"),
            (lambda d: d["routes"][0].update(stops=["S1", "NOPE"]), "unknown stop NOPE"),
            (lambda d: d["routes"][0].update(stops=[]), "list of 1 to"),
            (lambda d: d["routes"][0].update(path=[[0, 0]]), "at least 2 points"),
            (lambda d: d["routes"][0].update(path=[[0, 0], [200, 0]]), "out of range"),
            (lambda d: d["routes"].append(dict(d["routes"][0])), "route T1 is repeated"),
            (lambda d: d["buses"][0].update(seats=-1), "seats must be an integer"),
            (lambda d: d["buses"][0].update(seats=True), "seats must be an integer"),
            (lambda d: d["buses"][0].update(status="broken"), "status must be"),
            (lambda d: d["buses"][0].update(seats=0), "seats must be positive"),
            (lambda d: d["buses"][0].update(bus_id="../x"), "bus_id must match"),
            (lambda d: d["buses"].append(dict(d["buses"][0])), "bus 9001 is repeated"),
            (
                lambda d: d["buses"][0].update(
                    positions=[{"kind": "roof", "row_no": 1, "side": "left", "camera": "front"}]
                ),
                "roof",
            ),
            (lambda d: d["buses"][0].update(positions="nope"), "positions must be a list"),
        ],
    )
    def test_each_kind_of_bad_data_is_reported(self, mutate: Any, expected: str) -> None:
        data = package_data()
        mutate(data)
        with pytest.raises(CatalogPackageError) as error:
            parse_package(data)
        assert any(expected in problem for problem in error.value.problems), error.value.problems

    def test_every_problem_is_listed_not_just_the_first(self) -> None:
        data = package_data()
        data["stops"][0]["lat"] = 999
        data["buses"][0]["seats"] = -3
        with pytest.raises(CatalogPackageError) as error:
            parse_package(data)
        assert len(error.value.problems) >= 2

    def test_size_limits_protect_against_huge_files(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from core_service.ingestion import package as module

        monkeypatch.setattr(module, "MAX_STOPS", 2)
        with pytest.raises(CatalogPackageError, match="at most 2"):
            parse_package(package_data())

    def test_positions_can_be_supplied_explicitly(self) -> None:
        data = package_data()
        layout = default_layout(BusCapacity(41, 8, 0, 2))
        data["buses"][0]["positions"] = [
            {"kind": p.kind.value, "row_no": p.row_no, "side": p.side.value, "camera": p.camera.value, "roi": p.roi}
            for p in layout
        ]
        parsed = parse_package(data)
        assert parsed.buses[0].positions is not None and len(parsed.buses[0].positions) == len(layout)


# ---------------------------------------------------------------------------- the adapter
def rutas_js(routes: list[dict[str, Any]]) -> str:
    return (
        "/** header */\nconst RUTAS_PASTO = "
        + json.dumps(routes, indent=2)
        + ";\nif (typeof module !== 'undefined') {}\n"
    )


def source_route(route_id: str = "T1", **overrides: Any) -> dict[str, Any]:
    path = straight_path()
    stops = [
        {"id": "10", "nombre": "Uno", "lat": 1.2, "lng": -77.298, "coordenadaVerificada": True},
        {"id": "11", "nombre": "Dos", "lat": 1.2, "lng": -77.290, "coordenadaVerificada": True},
        {"id": "12", "nombre": "Tres", "lat": 1.2, "lng": -77.283, "coordenadaVerificada": True},
    ]
    item = {
        "id": route_id,
        "origen": "A",
        "destino": "B",
        "paradasIda": stops,
        "paradasRetorno": list(reversed(stops)),
        "coordenadasIda": [[lat, lng] for lng, lat in path],
        "coordenadasRetorno": [[lat, lng] for lng, lat in reversed(path)],
    }
    item.update(overrides)
    return item


class TestAvanteAdapter:
    def test_the_javascript_wrapper_is_read_and_coordinates_are_converted_to_lng_lat(self) -> None:
        package = load_avante_routes(rutas_js([source_route()]), ["T1"])
        route = package.routes[0]
        assert route.stop_ids == ["AV-10", "AV-11", "AV-12"] and route.return_stop_ids == ["AV-12", "AV-11", "AV-10"]
        assert route.path[0] == GeoPoint(1.2, -77.30) and route.name == "A - B"
        assert package.stops["AV-10"].location == GeoPoint(1.2, -77.298)
        assert "RutiPasto" in package.provenance["source"] and package.provenance["license_note"]

    def test_plain_json_is_accepted_too(self) -> None:
        assert len(parse_rutas_js(json.dumps([source_route()]))) == 1

    def test_a_missing_route_is_reported(self) -> None:
        with pytest.raises(CatalogPackageError, match="not found in the source: Z9"):
            load_avante_routes(rutas_js([source_route()]), ["T1", "Z9"])

    @pytest.mark.parametrize("text", ["const X = 1;", "const RUTAS_PASTO = [ {broken", "const RUTAS_PASTO = {};"])
    def test_a_file_without_a_readable_array_is_refused(self, text: str) -> None:
        with pytest.raises(CatalogPackageError):
            load_avante_routes(text, ["T1"])

    def test_the_same_code_at_two_places_is_a_conflict(self) -> None:
        second = source_route("T2")
        second["paradasIda"][0]["lat"] = 1.21  # ~1 km away, same code
        with pytest.raises(CatalogPackageError, match="two different places"):
            load_avante_routes(rutas_js([source_route(), second]), ["T1", "T2"])

    def test_a_different_spelling_of_the_same_stop_keeps_the_first_name(self) -> None:
        second = source_route("T2")
        second["paradasIda"][0]["nombre"] = "UNO (otra grafía)"
        package = load_avante_routes(rutas_js([source_route(), second]), ["T1", "T2"])
        assert package.stops["AV-10"].name == "Uno" and "spelled differently" in package.provenance["notes"]

    def test_stops_without_verified_coordinates_are_left_out(self) -> None:
        route = source_route()
        route["paradasIda"][1]["coordenadaVerificada"] = False
        package = load_avante_routes(rutas_js([route]), ["T1"])
        assert package.routes[0].stop_ids == ["AV-10", "AV-12"] and "without verified" in package.provenance["notes"]


# ----------------------------------------------------------------------------- the loader
class TestLoader:
    def test_everything_is_written_with_both_directions_progress_and_maps(
        self, loader: CatalogLoader, repo: CatalogRepository, db: Database
    ) -> None:
        report = loader.ingest(parse_package(package_data()))
        assert report.ok and report.applied
        assert (report.stops_created, report.routes_created, report.buses_created) == (4, 1, 2)
        assert report.positions_written == 51 + 31
        route = repo.load_routes()[0]
        assert [s.stop_id for s in route.stops] == ["S1", "S2", "S3", "S4"] and route.return_is_mirror
        progress = repo.stop_progress("T1", Direction.OUTBOUND)
        assert list(progress) == ["S1", "S2", "S3", "S4"] and all(v is not None for v in progress.values())
        assert progress["S1"] < progress["S2"] < progress["S3"] < progress["S4"]
        back = repo.stop_progress("T1", Direction.RETURN)
        assert list(back) == ["S4", "S3", "S2", "S1"] and back["S4"] < back["S1"]
        assert db.query_one("SELECT count(*) AS n FROM bus_positions WHERE bus_id = '9001'") == {"n": 51}

    def test_running_it_twice_gives_the_same_data(self, loader: CatalogLoader, db: Database) -> None:
        package = parse_package(package_data())
        loader.ingest(package)
        before = _row_counts(db)
        second = loader.ingest(package)
        after = _row_counts(db)
        assert before == after
        assert (second.stops_updated, second.routes_updated, second.buses_updated) == (4, 1, 2)
        assert second.stops_created == second.routes_created == second.buses_created == 0

    def test_a_dry_run_reports_but_writes_nothing(self, loader: CatalogLoader, db: Database) -> None:
        report = loader.ingest(parse_package(package_data()), dry_run=True)
        assert report.dry_run and not report.applied and report.routes_created == 1 and report.positions_written == 82
        assert db.query_one("SELECT count(*) AS n FROM routes") == {"n": 0}
        assert db.query_one("SELECT count(*) AS n FROM stops") == {"n": 0}

    def test_the_catalog_service_can_load_what_was_ingested(
        self, loader: CatalogLoader, repo: CatalogRepository
    ) -> None:
        loader.ingest(parse_package(package_data()))
        catalog = RouteCatalog()
        report = CatalogService(repo, catalog, MetricsRegistry()).reload()
        assert report.routes_loaded == 1 and report.buses_loaded == 2 and not report.invalid_routes
        assert catalog.require_track("T1").stops()[2].stop.stop_id == "S3"

    # ------------------------------------------------------------------ data repairs
    def test_a_repeated_stop_keeps_only_its_first_appearance(
        self, loader: CatalogLoader, repo: CatalogRepository
    ) -> None:
        data = package_data()
        data["routes"][0]["stops"] = ["S1", "S2", "S2", "S3", "S4"]
        report = loader.ingest(parse_package(data))
        assert report.ok and any("listed again" in line for line in report.repairs)
        assert [s.stop_id for s in repo.load_routes()[0].stops] == ["S1", "S2", "S3", "S4"]

    def test_a_stop_far_from_the_trace_is_dropped_and_reported(
        self, loader: CatalogLoader, repo: CatalogRepository
    ) -> None:
        data = package_data()
        data["stops"].append(stop_at("FAR", -77.29, lat=1.21))  # ~1 km north of the trace
        data["routes"][0]["stops"] = ["S1", "S2", "FAR", "S3", "S4"]
        report = loader.ingest(parse_package(data))
        assert any("FAR" in line and "from the trace" in line for line in report.repairs)
        assert "FAR" not in [s.stop_id for s in repo.load_routes()[0].stops]

    def test_a_stop_listed_out_of_order_is_dropped_and_reported(
        self, loader: CatalogLoader, repo: CatalogRepository
    ) -> None:
        data = package_data()
        data["routes"][0]["stops"] = ["S1", "S3", "S2", "S4"]
        report = loader.ingest(parse_package(data))
        assert any("S2" in line and "out of order" in line for line in report.repairs)
        assert [s.stop_id for s in repo.load_routes()[0].stops] == ["S1", "S3", "S4"]

    def test_strict_mode_rejects_a_route_that_needs_repairs_and_writes_nothing(
        self, loader: CatalogLoader, db: Database
    ) -> None:
        data = package_data()
        data["routes"][0]["stops"] = ["S1", "S3", "S2", "S4"]
        report = loader.ingest(parse_package(data), strict=True)
        assert (
            not report.ok and not report.applied and any("strict" in line for line in report.errors + report.warnings)
        )
        assert db.query_one("SELECT count(*) AS n FROM routes") == {"n": 0}

    def test_a_route_with_fewer_than_two_usable_stops_is_rejected_but_the_others_load(
        self, loader: CatalogLoader, repo: CatalogRepository
    ) -> None:
        data = package_data()
        data["stops"].append(stop_at("FAR1", -77.29, lat=1.3))
        data["stops"].append(stop_at("FAR2", -77.28, lat=1.3))
        data["routes"].append({"route_id": "T2", "name": "Rota", "path": straight_path(), "stops": ["FAR1", "FAR2"]})
        data["buses"].append(bus("9100", "T2"))
        report = loader.ingest(parse_package(data))
        assert (
            not report.ok
            and any("route T2" in e for e in report.errors)
            and any("bus 9100" in e for e in report.errors)
        )
        assert report.applied and [r.route_id for r in repo.load_routes()] == ["T1"]

    def test_return_stops_and_trace_of_its_own_are_stored_and_reported(
        self, loader: CatalogLoader, repo: CatalogRepository
    ) -> None:
        data = package_data()
        route = data["routes"][0]
        route["return_path"] = [[lng, 1.2004] for lng, _ in reversed(straight_path())]  # a parallel street, ~44 m north
        data["stops"].append(stop_at("R1", -77.284, lat=1.2004))
        data["stops"].append(stop_at("R2", -77.292, lat=1.2004))
        route["return_stops"] = ["R1", "R2"]
        report = loader.ingest(parse_package(data))
        assert report.ok and any("return trip differs" in w for w in report.warnings)
        stored = repo.load_routes()[0]
        assert [s.stop_id for s in stored.return_stops] == ["R1", "R2"] and not stored.return_is_mirror
        back = repo.stop_progress("T1", Direction.RETURN)
        assert back["R1"] is not None and back["R1"] < back["R2"]  # R1 comes first on the way back

    # -------------------------------------------------------------------------- buses
    def test_a_bus_of_an_unknown_route_is_rejected(self, loader: CatalogLoader, db: Database) -> None:
        data = package_data()
        data["buses"].append(bus("9200", "NOPE"))
        report = loader.ingest(parse_package(data))
        assert any("bus 9200" in e and "NOPE" in e for e in report.errors)
        assert db.query_one("SELECT count(*) AS n FROM buses WHERE bus_id = '9200'") == {"n": 0}

    def test_a_bus_can_belong_to_a_route_that_already_exists_in_the_database(self, loader: CatalogLoader) -> None:
        loader.ingest(parse_package(package_data()))
        only_buses = {"format": "core-catalog/1", "buses": [bus("9300")]}
        assert loader.ingest(parse_package(only_buses)).ok

    def test_a_bus_without_a_route_is_allowed(self, loader: CatalogLoader) -> None:
        assert loader.ingest(parse_package({"buses": [bus("9400", None)]})).ok

    def test_positions_that_do_not_match_the_capacity_are_rejected(self, loader: CatalogLoader) -> None:
        data = package_data()
        layout = default_layout(BusCapacity(40, 8, 0, 2))
        data["buses"][0]["positions"] = [
            {"kind": p.kind.value, "row_no": p.row_no, "side": p.side.value, "camera": p.camera.value, "roi": p.roi}
            for p in layout
        ]
        report = loader.ingest(parse_package(data))
        assert any("bus 9001" in e and "seat: 40 mapped vs 41" in e for e in report.errors)

    def test_an_impossible_standing_count_is_reported_not_raised(self, loader: CatalogLoader) -> None:
        data = package_data()
        data["buses"][0].update(seats=12, standing_positions=9)
        assert any("do not fit" in e for e in loader.ingest(parse_package(data)).errors)

    def test_calibrated_positions_are_never_overwritten(self, loader: CatalogLoader, repo: CatalogRepository) -> None:
        loader.ingest(parse_package(package_data()))
        polygon = [[0, 0], [10, 0], [10, 10]]
        layout = repo.load_bus_positions("9001")
        from core_service.models import BusPosition

        first = layout[0]
        layout[0] = BusPosition(
            first.kind, first.row_no, first.side, first.camera, {"calibrated": True, "polygon": polygon}
        )
        repo.save_bus_positions("9001", layout)
        report = loader.ingest(parse_package(package_data()))
        assert report.ok and any("calibrated positions kept" in w for w in report.warnings)
        assert repo.load_bus_positions("9001")[0].roi["polygon"] == polygon

    def test_a_capacity_change_of_a_calibrated_bus_needs_a_new_calibration(
        self, loader: CatalogLoader, repo: CatalogRepository
    ) -> None:
        loader.ingest(parse_package(package_data()))
        from core_service.models import BusPosition

        layout = repo.load_bus_positions("9001")
        first = layout[0]
        layout[0] = BusPosition(
            first.kind,
            first.row_no,
            first.side,
            first.camera,
            {"calibrated": True, "polygon": [[0, 0], [1, 0], [1, 1]]},
        )
        repo.save_bus_positions("9001", layout)
        data = package_data()
        data["buses"][0]["seats"] = 43
        report = loader.ingest(parse_package(data))
        assert any("recalibrate" in e for e in report.errors)
        assert repo.load_bus_positions("9001")[0].calibrated

    def test_a_failure_in_the_middle_leaves_the_database_untouched(
        self, loader: CatalogLoader, repo: CatalogRepository, db: Database, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        original = repo.save_bus
        calls = {"n": 0}

        def flaky(item: Any) -> None:
            calls["n"] += 1
            if calls["n"] == 2:
                raise RuntimeError("disk on fire")
            original(item)

        monkeypatch.setattr(repo, "save_bus", flaky)
        with pytest.raises(RuntimeError):
            loader.ingest(parse_package(package_data()))
        assert _row_counts(db) == [{"n": 0}] * len(_COUNT_QUERIES)


# ----------------------------------------------------------------------- the real package
class TestRealPastoPackage:
    """The committed package (routes C1-C4 from public data + a demo fleet) against the service's own rules."""

    def test_the_committed_files_are_valid_and_carry_their_provenance(self) -> None:
        package = load_package(REAL_PACKAGE.read_text(encoding="utf-8"))
        assert [r.route_id for r in package.routes] == ["C1", "C2", "C3", "C4"] and len(package.buses) == 8
        assert "RutiPasto" in package.provenance["source"] and "OpenStreetMap" in package.provenance["attribution"]
        assert package.provenance["fleet_source"].startswith("Flota de demostración")
        assert len(load_package(FLEET.read_text(encoding="utf-8")).buses) == 8

    def test_it_loads_and_the_service_accepts_all_four_routes(
        self, loader: CatalogLoader, repo: CatalogRepository, db: Database
    ) -> None:
        package = load_package(REAL_PACKAGE.read_text(encoding="utf-8"))
        report = loader.ingest(package)
        assert report.ok and report.applied and (report.routes_created, report.buses_created) == (4, 8)
        catalog = RouteCatalog()
        reloaded = CatalogService(repo, catalog, MetricsRegistry()).reload()
        assert reloaded.routes_loaded == 4 and reloaded.invalid_routes == [] and reloaded.buses_loaded == 8
        assert sorted(catalog.route_ids()) == ["C1", "C2", "C3", "C4"]
        for track in catalog.tracks():
            assert len(track) >= 30 and track.path.length_m > 5_000
        # every stop of both directions has a progress value, and the capacity check accepted every map
        assert db.query_one("SELECT count(*) AS n FROM route_stops WHERE progress IS NULL") == {"n": 0}
        assert db.query_one(
            "SELECT count(*) AS n FROM buses b WHERE NOT EXISTS (SELECT 1 FROM bus_positions p WHERE p.bus_id = b.bus_id)"
        ) == {"n": 0}

    def test_stops_shared_by_several_routes_are_stored_once(self, loader: CatalogLoader, db: Database) -> None:
        package = load_package(REAL_PACKAGE.read_text(encoding="utf-8"))
        loader.ingest(package)
        listed = sum(len(r.stop_ids) + len(r.return_stop_ids or []) for r in package.routes)
        assert (
            db.query_one("SELECT count(*) AS n FROM stops") == {"n": len(package.stops)} and len(package.stops) < listed
        )

    def test_postgis_confirms_the_stored_progress_of_the_real_routes(
        self, db: Database, loader: CatalogLoader, repo: CatalogRepository
    ) -> None:
        from core_service.persistence.relational import SpatialManager

        if db.query_one("SELECT 1 AS ok FROM pg_available_extensions WHERE name = 'postgis'") is None:
            pytest.skip("PostGIS no está instalado en este servidor")
        loader.ingest(load_package(REAL_PACKAGE.read_text(encoding="utf-8")))
        spatial = SpatialManager(db)
        assert spatial.configure().enabled
        assert spatial.progress_mismatches(0.005) == []


# ------------------------------------------------------------------------------------ CLI
class TestCommandLine:
    def _settings(self) -> tuple[str, str]:
        settings = database_settings()
        return settings.url, settings.schema

    def test_it_writes_a_package_from_public_data_without_a_database(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        source = tmp_path / "rutas.js"
        source.write_text(rutas_js([source_route()]), encoding="utf-8")
        out = tmp_path / "out.json"
        code = cli_main(["--source", "avante", "--file", str(source), "--routes", "T1", "--write-package", str(out)])
        assert code == 0 and "1 rutas" in capsys.readouterr().out
        assert load_package(out.read_text(encoding="utf-8")).routes[0].route_id == "T1"

    def test_the_avante_source_needs_the_list_of_routes(self, tmp_path: Path) -> None:
        source = tmp_path / "rutas.js"
        source.write_text(rutas_js([source_route()]), encoding="utf-8")
        assert cli_main(["--source", "avante", "--file", str(source)]) == 2

    def test_an_invalid_package_exits_with_a_usage_error(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        bad = tmp_path / "bad.json"
        bad.write_text(json.dumps({"stops": [{"stop_id": "x y"}]}), encoding="utf-8")
        assert cli_main(["--source", "package", "--file", str(bad), "--write-package", str(tmp_path / "o.json")]) == 2
        assert "no es válido" in capsys.readouterr().err

    def test_a_missing_file_is_a_usage_error(self, tmp_path: Path) -> None:
        assert cli_main(["--source", "package", "--file", str(tmp_path / "nope.json")]) == 2

    def test_loading_needs_a_database(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        monkeypatch.delenv("CORE_DATABASE_URL", raising=False)
        path = tmp_path / "p.json"
        path.write_text(json.dumps(package_data()), encoding="utf-8")
        assert cli_main(["--source", "package", "--file", str(path)]) == 2
        assert "CORE_DATABASE_URL" in capsys.readouterr().err

    def test_it_refuses_an_outdated_schema(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        url, schema = self._settings()
        import psycopg

        with psycopg.connect(url, autocommit=True) as connection:
            connection.execute(
                f'CREATE TABLE "{schema}".schema_migrations (version int primary key, description text, applied_at float8)'
            )
            connection.execute(
                sql.SQL("INSERT INTO {}.schema_migrations VALUES (2, 'old', 0)").format(sql.Identifier(schema))
            )
        path = tmp_path / "p.json"
        path.write_text(json.dumps(package_data()), encoding="utf-8")
        assert cli_main(["--source", "package", "--file", str(path), "--database-url", url, "--schema", schema]) == 2
        assert "desactualizado" in capsys.readouterr().err

    def test_dry_run_then_real_load_and_exit_codes(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        settings = database_settings()
        database = Database(settings.url, schema=settings.schema, min_size=1, max_size=1)
        try:
            from core_service.persistence.relational import Migrator

            Migrator(database).migrate()
            path = tmp_path / "p.json"
            path.write_text(json.dumps(package_data()), encoding="utf-8")
            common = [
                "--source",
                "package",
                "--file",
                str(path),
                "--database-url",
                settings.url,
                "--schema",
                settings.schema,
            ]
            assert cli_main([*common, "--dry-run"]) == 0
            assert "SIMULACIÓN" in capsys.readouterr().out
            assert database.query_one("SELECT count(*) AS n FROM routes") == {"n": 0}
            assert cli_main(common) == 0
            output = capsys.readouterr().out
            assert "APLICADO" in output and "catalog/reload" in output
            assert database.query_one("SELECT count(*) AS n FROM routes") == {"n": 1}
            broken = copy.deepcopy(package_data())
            broken["buses"].append(bus("9500", "NOPE"))
            path.write_text(json.dumps(broken), encoding="utf-8")
            assert cli_main(common) == 1  # loaded what it could, exit code tells there were errors
        finally:
            database.close()
