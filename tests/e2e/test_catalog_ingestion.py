"""Ingest the real C1-C4 package, reload the running service through the API and drive a bus on a new route."""

from __future__ import annotations

from pathlib import Path

from core_service.ingestion import CatalogLoader, load_package
from core_service.models import Role
from system_harness import API, INGEST, System, build_system

PACKAGE = Path(__file__).resolve().parents[2] / "demo_data" / "pasto_c1_c4.json"


def ingest_package(system: System):
    package = load_package(PACKAGE.read_text(encoding="utf-8"))
    return CatalogLoader(system.context.database, system.context.catalog_repository).ingest(package)


class TestCatalogIngestion:
    def test_new_routes_appear_after_an_administrator_reloads_the_catalog(self, system: System) -> None:
        admin = system.staff(Role.ADMIN, "admin@example.com")
        before = {r["route_id"] for r in system.client.get(f"{API}/routes", headers=admin).json()["routes"]}
        assert not {"C1", "C2", "C3", "C4"} & before

        report = ingest_package(system)
        assert report.ok and report.routes_created == 4 and report.buses_created == 8
        # the running service does not see it until it is asked to reload
        still = {r["route_id"] for r in system.client.get(f"{API}/routes", headers=admin).json()["routes"]}
        assert still == before

        reloaded = system.client.post(f"{API}/admin/catalog/reload", headers=admin)
        assert reloaded.status_code == 200 and reloaded.json()["invalid_routes"] == []
        after = {r["route_id"] for r in system.client.get(f"{API}/routes", headers=admin).json()["routes"]}
        assert {"C1", "C2", "C3", "C4"} <= after

    def test_a_bus_of_an_ingested_route_is_tracked_along_its_real_path(self, system: System) -> None:
        admin = system.staff(Role.ADMIN, "admin@example.com")
        ingest_package(system)
        assert system.client.post(f"{API}/admin/catalog/reload", headers=admin).status_code == 200

        route = system.client.get(f"{API}/routes/C1", headers=admin).json()
        path = route["path"]
        point = path[len(path) // 3]
        lat, lng = (point["lat"], point["lng"]) if isinstance(point, dict) else (point[1], point[0])
        response = system.client.post(
            f"{API}/internal/buses/2101/telemetry",
            json={
                "ts": system.clock.now(),
                "lat": lat,
                "lng": lng,
                "seated": 20,
                "standing": 3,
                "doors_total": 23,
                "confidence": 0.95,
                "model_version": "occ-1.2",
                "speed_kmh": 25.0,
            },
            headers=INGEST,
        )
        assert response.status_code == 200, response.text
        active = system.client.get(f"{API}/buses/active", params={"route_id": "C1"}, headers=admin).json()
        assert [b["bus_id"] for b in active["routes"][0]["buses"]] == ["2101"]

    def test_only_administrators_can_reload_the_catalog(self, system: System) -> None:
        assert system.client.post(f"{API}/admin/catalog/reload").status_code == 401
        assert system.client.post(f"{API}/admin/catalog/reload", headers=system.guest()).status_code == 403
        auditor = system.staff(Role.AUDITOR, "aud@example.com")
        assert system.client.post(f"{API}/admin/catalog/reload", headers=auditor).status_code == 403

    def test_a_second_ingestion_changes_nothing(self) -> None:
        system = build_system()
        first = ingest_package(system)
        second = ingest_package(system)
        assert first.routes_created == 4
        assert second.routes_created == 0 and second.buses_created == 0 and second.stops_created == 0
