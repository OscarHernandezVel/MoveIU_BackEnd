"""Catalogue package: the interchange format for routes, stops and buses ("core-catalog/1").

A package is a plain JSON document. Anything that can produce it (an adapter for a transit agency's
data, a GTFS converter, a hand-written file) can feed the loader, and the loader never trusts it: every
field is validated here, with limits on sizes so that a huge or hostile file cannot exhaust memory.

.. code-block:: json

    {
      "format": "core-catalog/1",
      "provenance": {"source": "...", "retrieved_at": "2026-09-29", "license_note": "...", "attribution": "..."},
      "stops":  [{"stop_id": "AV-4270", "name": "Obonuco", "lat": 1.19327, "lng": -77.30664}],
      "routes": [{"route_id": "C1", "name": "Obonuco - Altos de Chapalito",
                  "path": [[-77.30, 1.19], [-77.29, 1.20]],         // [lng, lat], as GeoJSON
                  "stops": ["AV-4270"],                              // outbound order
                  "return_path": [[-77.29, 1.20], [-77.30, 1.19]],   // optional
                  "return_stops": ["AV-4270"]}],                     // optional (default: outbound reversed)
      "buses":  [{"bus_id": "2101", "route_id": "C1", "model": "Bus 12 m", "seats": 41,
                  "standing_positions": 8, "wheelchair_spaces": 0, "door_zones": 2, "status": "active",
                  "positions": [{"kind": "seat", "row_no": 1, "side": "left", "camera": "front", "roi": {}}]}]
    }

``positions`` is optional: without it the loader generates a provisional interior map (see
``occupancy/layout.py``) marked as not calibrated.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from core_service.errors import ValidationError
from core_service.models import (
    Bus,
    BusCapacity,
    BusPosition,
    BusStatus,
    Camera,
    GeoPoint,
    PositionKind,
    PositionSide,
    Stop,
)

FORMAT = "core-catalog/1"

MAX_STOPS = 50_000
MAX_ROUTES = 500
MAX_BUSES = 20_000
MAX_PATH_POINTS = 100_000
MAX_POSITIONS_PER_BUS = 200
MAX_STOPS_PER_ROUTE = 1_000

STOP_ID = re.compile(r"^[A-Za-z0-9._-]{1,20}$")
ROUTE_ID = re.compile(r"^[A-Za-z0-9-]{1,10}$")
BUS_ID = re.compile(r"^[A-Za-z0-9-]{1,20}$")


class CatalogPackageError(Exception):
    """The package is malformed. ``problems`` lists every issue found (not just the first)."""

    def __init__(self, problems: Sequence[str]) -> None:
        super().__init__("; ".join(problems[:5]) + (" ..." if len(problems) > 5 else ""))
        self.problems = list(problems)


@dataclass(frozen=True)
class PackageRoute:
    route_id: str
    name: str
    path: list[GeoPoint]
    stop_ids: list[str]
    return_path: list[GeoPoint] | None = None
    return_stop_ids: list[str] | None = None


@dataclass(frozen=True)
class PackageBus:
    bus: Bus
    positions: list[BusPosition] | None = None


@dataclass(frozen=True)
class CatalogPackage:
    stops: dict[str, Stop] = field(default_factory=dict)
    routes: list[PackageRoute] = field(default_factory=list)
    buses: list[PackageBus] = field(default_factory=list)
    provenance: dict[str, str] = field(default_factory=dict)

    # ------------------------------------------------------------------ export
    def to_dict(self) -> dict[str, Any]:
        def path(points: Sequence[GeoPoint]) -> list[list[float]]:
            return [[p.lng, p.lat] for p in points]

        routes = []
        for route in self.routes:
            item: dict[str, Any] = {
                "route_id": route.route_id,
                "name": route.name,
                "path": path(route.path),
                "stops": route.stop_ids,
            }
            if route.return_path is not None:
                item["return_path"] = path(route.return_path)
            if route.return_stop_ids is not None:
                item["return_stops"] = route.return_stop_ids
            routes.append(item)
        buses = []
        for entry in self.buses:
            capacity = entry.bus.capacity
            record: dict[str, Any] = {
                "bus_id": entry.bus.bus_id,
                "route_id": entry.bus.route_id,
                "model": entry.bus.model,
                "seats": capacity.seats,
                "standing_positions": capacity.standing_positions,
                "wheelchair_spaces": capacity.wheelchair_spaces,
                "door_zones": capacity.door_zones,
                "status": entry.bus.status.value,
            }
            if entry.positions is not None:
                record["positions"] = [
                    {
                        "kind": p.kind.value,
                        "row_no": p.row_no,
                        "side": p.side.value,
                        "camera": p.camera.value,
                        "roi": p.roi,
                    }
                    for p in entry.positions
                ]
            buses.append(record)
        return {
            "format": FORMAT,
            "provenance": self.provenance,
            "stops": [
                {"stop_id": s.stop_id, "name": s.name, "lat": s.location.lat, "lng": s.location.lng}
                for s in self.stops.values()
            ],
            "routes": routes,
            "buses": buses,
        }

    def dumps(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, separators=(",", ":"))


# ---------------------------------------------------------------------------------------- parsing
class _Problems:
    def __init__(self) -> None:
        self.items: list[str] = []

    def add(self, where: str, message: str) -> None:
        if len(self.items) < 200:
            self.items.append(f"{where}: {message}")


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return float(value)


def _point(raw: Any, where: str, problems: _Problems) -> GeoPoint | None:
    """A ``[lng, lat]`` pair (GeoJSON order)."""
    if not isinstance(raw, list | tuple) or len(raw) != 2:
        problems.add(where, "a point must be [lng, lat]")
        return None
    lng, lat = _number(raw[0]), _number(raw[1])
    if lng is None or lat is None or not (-180 <= lng <= 180 and -90 <= lat <= 90):
        problems.add(where, "coordinates out of range")
        return None
    return GeoPoint(lat, lng)


def _path(raw: Any, where: str, problems: _Problems) -> list[GeoPoint] | None:
    if not isinstance(raw, list) or len(raw) < 2:
        problems.add(where, "a path needs at least 2 points")
        return None
    if len(raw) > MAX_PATH_POINTS:
        problems.add(where, f"more than {MAX_PATH_POINTS} points")
        return None
    points = [_point(item, f"{where}[{i}]", problems) for i, item in enumerate(raw)]
    return None if any(p is None for p in points) else [p for p in points if p is not None]


def _text(item: Mapping[str, Any], key: str, where: str, problems: _Problems, limit: int = 100) -> str | None:
    value = item.get(key)
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        problems.add(where, f"{key} must be a non-empty text of at most {limit} characters")
        return None
    return value.strip()


def _int(item: Mapping[str, Any], key: str, where: str, problems: _Problems, default: int | None = None) -> int | None:
    value = item.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0 or value > 1000:
        problems.add(where, f"{key} must be an integer between 0 and 1000")
        return None
    return value


def _ids(raw: Any, pattern: re.Pattern[str], where: str, problems: _Problems) -> list[str] | None:
    if not isinstance(raw, list) or not raw or len(raw) > MAX_STOPS_PER_ROUTE:
        problems.add(where, f"must be a list of 1 to {MAX_STOPS_PER_ROUTE} stop ids")
        return None
    if not all(isinstance(i, str) and pattern.match(i) for i in raw):
        problems.add(where, "contains an invalid stop id")
        return None
    return list(raw)


def _positions(raw: Any, where: str, problems: _Problems) -> list[BusPosition] | None:
    if not isinstance(raw, list) or len(raw) > MAX_POSITIONS_PER_BUS:
        problems.add(where, f"positions must be a list of at most {MAX_POSITIONS_PER_BUS}")
        return None
    result: list[BusPosition] = []
    for index, item in enumerate(raw):
        label = f"{where}[{index}]"
        if not isinstance(item, dict):
            problems.add(label, "must be an object")
            continue
        try:
            roi = item.get("roi", {"calibrated": False, "polygon": []})
            if not isinstance(roi, dict):
                raise ValueError("roi must be an object")
            result.append(
                BusPosition(
                    PositionKind(item.get("kind")),
                    int(item.get("row_no", 0)),
                    PositionSide(item.get("side")),
                    Camera(item.get("camera")),
                    roi if roi else {"calibrated": False, "polygon": []},
                )
            )
        except (ValueError, TypeError, ValidationError) as error:
            problems.add(label, str(error) or "invalid position")
    return result


def parse_package(data: Any) -> CatalogPackage:
    """Validate ``data`` (already decoded JSON) and build a :class:`CatalogPackage`."""
    problems = _Problems()
    if not isinstance(data, dict):
        raise CatalogPackageError(["the package must be a JSON object"])
    declared = data.get("format", FORMAT)
    if declared != FORMAT:
        raise CatalogPackageError([f"unsupported format {declared!r}, expected {FORMAT!r}"])
    raw_stops, raw_routes, raw_buses = data.get("stops", []), data.get("routes", []), data.get("buses", [])
    for name, value, limit in (
        ("stops", raw_stops, MAX_STOPS),
        ("routes", raw_routes, MAX_ROUTES),
        ("buses", raw_buses, MAX_BUSES),
    ):
        if not isinstance(value, list) or len(value) > limit:
            raise CatalogPackageError([f"{name} must be a list of at most {limit} items"])

    stops: dict[str, Stop] = {}
    for index, item in enumerate(raw_stops):
        where = f"stops[{index}]"
        if not isinstance(item, dict):
            problems.add(where, "must be an object")
            continue
        stop_id, stop_name = item.get("stop_id"), _text(item, "name", where, problems)
        lat, lng = _number(item.get("lat")), _number(item.get("lng"))
        if not isinstance(stop_id, str) or not STOP_ID.match(stop_id):
            problems.add(where, "stop_id must match [A-Za-z0-9._-]{1,20}")
        elif lat is None or lng is None or not (-90 <= lat <= 90 and -180 <= lng <= 180):
            problems.add(where, "lat/lng out of range")
        elif stop_id in stops:
            problems.add(where, f"stop {stop_id} is repeated")
        elif stop_name:
            stops[stop_id] = Stop(stop_id, stop_name, GeoPoint(lat, lng))

    routes: list[PackageRoute] = []
    seen_routes: set[str] = set()
    for index, item in enumerate(raw_routes):
        where = f"routes[{index}]"
        if not isinstance(item, dict):
            problems.add(where, "must be an object")
            continue
        route_id = item.get("route_id")
        if not isinstance(route_id, str) or not ROUTE_ID.match(route_id):
            problems.add(where, "route_id must match [A-Za-z0-9-]{1,10}")
            continue
        if route_id in seen_routes:
            problems.add(where, f"route {route_id} is repeated")
            continue
        seen_routes.add(route_id)
        route_name = _text(item, "name", where, problems)
        path = _path(item.get("path"), f"{where}.path", problems)
        stop_ids = _ids(item.get("stops"), STOP_ID, f"{where}.stops", problems)
        back_path = _path(item["return_path"], f"{where}.return_path", problems) if item.get("return_path") else None
        back_ids = (
            _ids(item["return_stops"], STOP_ID, f"{where}.return_stops", problems) if "return_stops" in item else None
        )
        if route_name is None or path is None or stop_ids is None:
            continue
        for stop_id in [*stop_ids, *(back_ids or [])]:
            if stop_id not in stops:
                problems.add(where, f"unknown stop {stop_id}")
                break
        else:
            routes.append(PackageRoute(route_id, route_name, path, stop_ids, back_path, back_ids))

    buses: list[PackageBus] = []
    seen_buses: set[str] = set()
    for index, item in enumerate(raw_buses):
        where = f"buses[{index}]"
        if not isinstance(item, dict):
            problems.add(where, "must be an object")
            continue
        bus_id, route_id = item.get("bus_id"), item.get("route_id")
        if not isinstance(bus_id, str) or not BUS_ID.match(bus_id):
            problems.add(where, "bus_id must match [A-Za-z0-9-]{1,20}")
            continue
        if bus_id in seen_buses:
            problems.add(where, f"bus {bus_id} is repeated")
            continue
        seen_buses.add(bus_id)
        if route_id is not None and (not isinstance(route_id, str) or not ROUTE_ID.match(route_id)):
            problems.add(where, "route_id must match [A-Za-z0-9-]{1,10}")
            continue
        model = _text(item, "model", where, problems, 50)
        seats = _int(item, "seats", where, problems)
        standing = _int(item, "standing_positions", where, problems)
        chairs = _int(item, "wheelchair_spaces", where, problems, 0)
        doors = _int(item, "door_zones", where, problems, 2)
        try:
            status = BusStatus(item.get("status", "active"))
        except ValueError:
            problems.add(where, "status must be active, inactive or maintenance")
            continue
        positions = _positions(item["positions"], f"{where}.positions", problems) if "positions" in item else None
        if None in (model, seats, standing, chairs, doors):
            continue
        try:
            capacity = BusCapacity(seats or 0, standing or 0, chairs or 0, doors or 0)
        except ValidationError as error:
            problems.add(where, "; ".join(error.details) or "invalid capacity")
            continue
        buses.append(PackageBus(Bus(bus_id, route_id, model or "", capacity, status), positions))

    if problems.items:
        raise CatalogPackageError(problems.items)
    provenance = data.get("provenance", {})
    clean = {str(k): str(v) for k, v in provenance.items()} if isinstance(provenance, dict) else {}
    return CatalogPackage(stops, routes, buses, clean)


def load_package(text: str) -> CatalogPackage:
    try:
        data = json.loads(text)
    except json.JSONDecodeError as error:
        raise CatalogPackageError([f"not valid JSON: {error.msg} (line {error.lineno})"]) from error
    return parse_package(data)
