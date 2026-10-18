"""Routes, stops and buses."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field

from psycopg.types.json import Jsonb

from core_service.models import (
    Bus,
    BusCapacity,
    BusPosition,
    BusStatus,
    Camera,
    Direction,
    GeoPoint,
    PositionKind,
    PositionSide,
    Stop,
)
from core_service.persistence.relational.database import Database, Row


@dataclass(frozen=True)
class RouteDefinition:
    """A route as stored: its trace and the ordered stops of each direction.

    ``return_path`` is ``None`` when the return trip uses the outbound trace backwards. ``return_stops``
    is always filled (the reverse of ``stops`` unless the route declares its own return stops).
    """

    route_id: str
    name: str
    path: list[GeoPoint]
    stops: list[Stop]
    return_path: list[GeoPoint] | None = None
    return_stops: list[Stop] = field(default_factory=list)

    @property
    def return_is_mirror(self) -> bool:
        """True when the return trip is exactly the outbound one walked backwards."""
        return self.return_path is None and [s.stop_id for s in self.return_stops] == [
            s.stop_id for s in reversed(self.stops)
        ]


def _path_json(path: Sequence[GeoPoint]) -> str:
    return json.dumps([[p.lng, p.lat] for p in path], separators=(",", ":"))


def _path(text: str) -> list[GeoPoint]:
    return [GeoPoint(lat, lng) for lng, lat in json.loads(text)]


def _bus(row: Row) -> Bus:
    return Bus(
        bus_id=row["bus_id"],
        route_id=row["route_id"],
        model=row["model"],
        capacity=BusCapacity(
            seats=row["seats"],
            standing_positions=row["standing_positions"],
            wheelchair_spaces=row["wheelchair_spaces"],
            door_zones=row["door_zones"],
        ),
        status=BusStatus(row["status"]),
    )


class CatalogRepository:
    def __init__(self, database: Database) -> None:
        self._db = database

    # ------------------------------------------------------------------ write
    def save_stop(self, stop: Stop) -> None:
        self._db.execute(
            "INSERT INTO stops (stop_id, name, lat, lng, active) VALUES (%s, %s, %s, %s, %s) "
            "ON CONFLICT (stop_id) DO UPDATE SET name = excluded.name, lat = excluded.lat, "
            "lng = excluded.lng, active = excluded.active",
            (stop.stop_id, stop.name, stop.location.lat, stop.location.lng, stop.active),
        )

    def save_route(
        self,
        route_id: str,
        name: str,
        path: Sequence[GeoPoint],
        stop_ids: Sequence[str],
        active: bool = True,
        *,
        return_path: Sequence[GeoPoint] | None = None,
        return_stop_ids: Sequence[str] | None = None,
    ) -> None:
        """Insert or replace a route with the ordered stops of both directions (atomic).

        Without ``return_stop_ids`` the return trip visits the same stops in reverse order; without
        ``return_path`` it uses the outbound trace backwards."""
        back_ids = list(return_stop_ids) if return_stop_ids is not None else list(reversed(stop_ids))
        with self._db.transaction():
            self._db.execute(
                "INSERT INTO routes (route_id, name, active, path_json, return_path_json) "
                "VALUES (%s, %s, %s, %s, %s) "
                "ON CONFLICT (route_id) DO UPDATE SET name = excluded.name, active = excluded.active, "
                "path_json = excluded.path_json, return_path_json = excluded.return_path_json",
                (route_id, name, active, _path_json(path), None if return_path is None else _path_json(return_path)),
            )
            self._db.execute("DELETE FROM route_stops WHERE route_id = %s", (route_id,))
            for direction, ids in ((Direction.OUTBOUND, stop_ids), (Direction.RETURN, back_ids)):
                for order, stop_id in enumerate(ids, start=1):
                    self._db.execute(
                        "INSERT INTO route_stops (route_id, direction, stop_order, stop_id) VALUES (%s, %s, %s, %s)",
                        (route_id, direction.value, order, stop_id),
                    )

    def set_stop_progress(self, route_id: str, direction: Direction, progress: Mapping[str, float]) -> int:
        """Store how far along the trace each stop is (0-1). Returns the rows updated."""
        updated = 0
        with self._db.transaction():
            for stop_id, value in progress.items():
                updated += self._db.execute(
                    "UPDATE route_stops SET progress = %s WHERE route_id = %s AND direction = %s AND stop_id = %s",
                    (min(1.0, max(0.0, value)), route_id, direction.value, stop_id),
                )
        return updated

    def save_bus_positions(self, bus_id: str, positions: Sequence[BusPosition]) -> None:
        """Replace the interior map of a bus. The counts must match the bus capacity (checked at commit)."""
        with self._db.transaction():
            self._db.execute("DELETE FROM bus_positions WHERE bus_id = %s", (bus_id,))
            for position in positions:
                self._db.execute(
                    "INSERT INTO bus_positions (bus_id, kind, row_no, side, camera, roi) "
                    "VALUES (%s, %s, %s, %s, %s, %s)",
                    (
                        bus_id,
                        position.kind.value,
                        position.row_no,
                        position.side.value,
                        position.camera.value,
                        Jsonb(position.roi),
                    ),
                )

    def load_bus_positions(self, bus_id: str) -> list[BusPosition]:
        rows = self._db.query_all(
            "SELECT kind, row_no, side, camera, roi FROM bus_positions WHERE bus_id = %s ORDER BY position_id",
            (bus_id,),
        )
        return [
            BusPosition(
                PositionKind(r["kind"]), r["row_no"], PositionSide(r["side"]), Camera(r["camera"]), dict(r["roi"])
            )
            for r in rows
        ]

    def save_bus(self, bus: Bus) -> None:
        self._db.execute(
            "INSERT INTO buses (bus_id, route_id, model, seats, standing_positions, wheelchair_spaces, "
            "door_zones, status) VALUES (%s, %s, %s, %s, %s, %s, %s, %s) "
            "ON CONFLICT (bus_id) DO UPDATE SET route_id = excluded.route_id, model = excluded.model, "
            "seats = excluded.seats, standing_positions = excluded.standing_positions, "
            "wheelchair_spaces = excluded.wheelchair_spaces, door_zones = excluded.door_zones, "
            "status = excluded.status",
            (
                bus.bus_id,
                bus.route_id,
                bus.model,
                bus.capacity.seats,
                bus.capacity.standing_positions,
                bus.capacity.wheelchair_spaces,
                bus.capacity.door_zones,
                bus.status.value,
            ),
        )

    # ------------------------------------------------------------------- read
    def _stops(self, route_id: str, direction: Direction) -> list[Stop]:
        rows = self._db.query_all(
            "SELECT s.* FROM route_stops rs JOIN stops s ON s.stop_id = rs.stop_id "
            "WHERE rs.route_id = %s AND rs.direction = %s ORDER BY rs.stop_order",
            (route_id, direction.value),
        )
        return [Stop(r["stop_id"], r["name"], GeoPoint(r["lat"], r["lng"]), bool(r["active"])) for r in rows]

    def load_routes(self) -> list[RouteDefinition]:
        definitions: list[RouteDefinition] = []
        for route in self._db.query_all("SELECT * FROM routes WHERE active ORDER BY route_id"):
            route_id = route["route_id"]
            back = route["return_path_json"]
            definitions.append(
                RouteDefinition(
                    route_id,
                    route["name"],
                    _path(route["path_json"]),
                    self._stops(route_id, Direction.OUTBOUND),
                    None if back is None else _path(back),
                    self._stops(route_id, Direction.RETURN),
                )
            )
        return definitions

    def stop_progress(self, route_id: str, direction: Direction) -> dict[str, float | None]:
        rows = self._db.query_all(
            "SELECT stop_id, progress FROM route_stops WHERE route_id = %s AND direction = %s ORDER BY stop_order",
            (route_id, direction.value),
        )
        return {r["stop_id"]: r["progress"] for r in rows}

    def load_buses(self) -> list[Bus]:
        return [_bus(row) for row in self._db.query_all("SELECT * FROM buses ORDER BY bus_id")]

    def find_bus(self, bus_id: str) -> Bus | None:
        row = self._db.query_one("SELECT * FROM buses WHERE bus_id = %s", (bus_id,))
        return None if row is None else _bus(row)
