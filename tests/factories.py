"""Builders shared by unit and end-to-end tests."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from core_service.models import Bus, BusCapacity, BusStatus, GeoPoint, Reading, Stop
from core_service.routing import PathGeometry, RouteTrack

DEMO_FILE = Path(__file__).resolve().parents[1] / "demo_data" / "route_c5.json"


def demo_data() -> dict[str, Any]:
    return json.loads(DEMO_FILE.read_text(encoding="utf-8"))


def demo_stops() -> dict[str, Stop]:
    return {
        item["stop_id"]: Stop(item["stop_id"], item["name"], GeoPoint(item["lat"], item["lng"]))
        for item in demo_data()["stops"]
    }


def demo_track() -> RouteTrack:
    data = demo_data()
    route = data["routes"][0]
    stops = demo_stops()
    path = PathGeometry(GeoPoint(lat, lng) for lng, lat in route["path"])
    return RouteTrack(route["route_id"], route["name"], path, [stops[s] for s in route["stops"]])


def demo_buses() -> list[Bus]:
    return [
        Bus(
            item["bus_id"],
            item["route_id"],
            item["model"],
            BusCapacity(item["seats"], item["standing_positions"], item["wheelchair_spaces"], item["door_zones"]),
            BusStatus(item["status"]),
        )
        for item in demo_data()["buses"]
    ]


BUS_41 = BusCapacity(seats=41, standing_positions=8)
BUS_ACCESSIBLE = BusCapacity(seats=41, standing_positions=8, wheelchair_spaces=1)
MINIBUS_25 = BusCapacity(seats=25, standing_positions=4)


def make_reading(
    *,
    bus_id: str = "1045",
    ts: float = 1_790_000_000.0,
    lat: float = 1.2136,
    lng: float = -77.2811,
    seated: int = 10,
    standing: int = 0,
    doors_total: int | None = None,
    confidence: float = 0.95,
    model_version: str = "occ-1.2",
    standing_out_of_position: int = 0,
    children_in_arms: int = 0,
    wheelchair_occupied: int = 0,
    approximate_count: bool = False,
    speed_kmh: float | None = 30.0,
) -> Reading:
    people = seated + standing + standing_out_of_position + wheelchair_occupied
    return Reading(
        bus_id=bus_id,
        ts=ts,
        location=GeoPoint(lat, lng),
        seated=seated,
        standing=standing,
        doors_total=people if doors_total is None else doors_total,
        confidence=confidence,
        model_version=model_version,
        standing_out_of_position=standing_out_of_position,
        children_in_arms=children_in_arms,
        wheelchair_occupied=wheelchair_occupied,
        approximate_count=approximate_count,
        speed_kmh=speed_kmh,
    )


# ------------------------------------------------------------------ live states
from core_service.models import Direction, LiveBusState, OccupancyState  # noqa: E402


def make_state(
    *,
    bus_id: str = "1045",
    route_id: str = "C5",
    direction: Direction | None = Direction.OUTBOUND,
    progress: float | None = 0.3,
    state: OccupancyState = OccupancyState.AVAILABLE_SEATS,
    pct: int = 50,
    off_route: bool = False,
    reading_ts: float = 1_790_000_000.0,
) -> LiveBusState:
    return LiveBusState(
        bus_id=bus_id,
        route_id=route_id,
        direction=direction,
        lat=1.2136,
        lng=-77.2811,
        speed_kmh=30.0,
        seated=20,
        standing=0,
        standing_out_of_position=0,
        people_on_board=20,
        occupancy_pct=pct,
        state=state,
        free_seats=21,
        free_standing=8,
        wheelchair_free=False,
        confidence=0.95,
        approximate_count=False,
        outbound_progress=progress,
        off_route=off_route,
        model_version="occ-1.2",
        reading_ts=reading_ts,
        updated_at=reading_ts,
    )
