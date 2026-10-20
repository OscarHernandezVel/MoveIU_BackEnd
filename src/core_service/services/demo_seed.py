"""Loads the demo network (route C5 and three buses) into the relational database."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from core_service.models import Bus, BusCapacity, BusStatus, GeoPoint, Stop
from core_service.persistence.relational import CatalogRepository

DEFAULT_FILE = Path(__file__).resolve().parents[3] / "demo_data" / "route_c5.json"


def load_demo_data(repository: CatalogRepository, data: dict[str, Any] | None = None) -> None:
    payload = data if data is not None else json.loads(DEFAULT_FILE.read_text(encoding="utf-8"))
    for item in payload["stops"]:
        repository.save_stop(Stop(item["stop_id"], item["name"], GeoPoint(item["lat"], item["lng"])))
    for route in payload["routes"]:
        path = [GeoPoint(lat, lng) for lng, lat in route["path"]]
        repository.save_route(route["route_id"], route["name"], path, route["stops"])
    for item in payload["buses"]:
        capacity = BusCapacity(item["seats"], item["standing_positions"], item["wheelchair_spaces"], item["door_zones"])
        repository.save_bus(Bus(item["bus_id"], item["route_id"], item["model"], capacity, BusStatus(item["status"])))
