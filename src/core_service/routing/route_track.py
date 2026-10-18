"""A route: its path plus its stops in a doubly linked list.

One list serves both directions. ``OUTBOUND`` walks ``next`` pointers, ``RETURN`` walks
``prev`` pointers, and the *travel progress* of a return trip is ``1 - outbound_progress``.

Data structures used
--------------------
* ``DoublyLinkedList`` of stops: next/previous stop in O(1), walking k stops in O(k).
* ``Array`` of stop progress values (sorted): the first stop ahead of a bus is found with
  a binary search in O(log n).
* Stops remaining to a given stop: index arithmetic over that same sorted array (O(log n), no walk),
  because the list position of a stop is its order in the route.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass

from core_service.data_structures import Array, DoublyLinkedList, Node
from core_service.errors import NotFoundError, ValidationError
from core_service.models import Direction, GeoPoint, Stop
from core_service.routing.geometry import PathGeometry

MAX_STOP_DISTANCE_M = 150.0


def return_geometry(path: Sequence[GeoPoint], return_path: Sequence[GeoPoint] | None) -> PathGeometry:
    """Trace of the return trip: its own when the route has one, else the outbound trace backwards."""
    return PathGeometry(list(return_path) if return_path else list(reversed(path)))


def stop_progress(geometry: PathGeometry, stops: Iterable[Stop]) -> dict[str, float]:
    """How far along ``geometry`` (0-1) each stop lies (the value stored in ``route_stops.progress``)."""
    result: dict[str, float] = {}
    last = -1.0
    for stop in stops:  # in route order: a stop on a street used twice is placed on the leg that comes next
        last = geometry.project_after(stop.location, last).progress
        result[stop.stop_id] = last
    return result


@dataclass(frozen=True)
class RouteStop:
    stop: Stop
    order: int
    progress: float  # outbound progress of the stop along the path (0-1)


class RouteTrack:
    def __init__(self, route_id: str, name: str, path: PathGeometry, stops: Iterable[Stop]) -> None:
        self.route_id = route_id
        self.name = name
        self.path = path
        self._stops: DoublyLinkedList[RouteStop] = DoublyLinkedList()
        self._nodes: Array[Node[RouteStop]] = Array()
        self._progress: Array[float] = Array()
        self._by_id: dict[str, Node[RouteStop]] = {}
        self._build(stops)

    # --------------------------------------------------------------- building
    def _build(self, stops: Iterable[Stop]) -> None:
        last_progress = -1.0
        for order, stop in enumerate(stops, start=1):
            projection = self.path.project_after(stop.location, last_progress)
            if projection.distance_m > MAX_STOP_DISTANCE_M:
                raise ValidationError(
                    "route_invalid_stops",
                    params={"route_id": self.route_id},
                    details=[f"stop {stop.stop_id} is {projection.distance_m:.0f} m away from the path"],
                )
            if projection.progress <= last_progress:
                raise ValidationError(
                    "route_invalid_stops",
                    params={"route_id": self.route_id},
                    details=[f"stop {stop.stop_id} is out of order along the path"],
                )
            if stop.stop_id in self._by_id:
                raise ValidationError(
                    "route_invalid_stops",
                    params={"route_id": self.route_id},
                    details=[f"stop {stop.stop_id} is repeated"],
                )
            node = self._stops.append(RouteStop(stop, order, projection.progress))
            self._nodes.append(node)
            self._progress.append(projection.progress)
            self._by_id[stop.stop_id] = node
            last_progress = projection.progress
        if len(self._stops) == 0:
            raise ValidationError("route_invalid_stops", params={"route_id": self.route_id}, details=["no stops"])

    # ---------------------------------------------------------------- queries
    def __len__(self) -> int:
        return len(self._stops)

    def stops(self) -> list[RouteStop]:
        """Stops in outbound order."""
        return self._stops.to_list()

    def find_stop(self, stop_id: str) -> RouteStop | None:
        node = self._by_id.get(stop_id)
        return None if node is None else node.value

    def require_stop(self, stop_id: str) -> RouteStop:
        found = self.find_stop(stop_id)
        if found is None:
            raise NotFoundError("stop_not_in_route", params={"route_id": self.route_id, "stop_id": stop_id})
        return found

    @staticmethod
    def travel_progress(direction: Direction, outbound_progress: float) -> float:
        """Progress measured in the bus' own travel direction (0 = its origin terminal)."""
        return outbound_progress if direction is Direction.OUTBOUND else 1.0 - outbound_progress

    @staticmethod
    def _step(node: Node[RouteStop], direction: Direction) -> Node[RouteStop] | None:
        return node.next if direction is Direction.OUTBOUND else node.prev

    def _first_ahead_index(self, direction: Direction, outbound_progress: float) -> int:
        """Index (outbound order) of the first stop strictly ahead of the bus; may be out of range
        (-1 or len) when no stop is left. Binary search: O(log n)."""
        if direction is Direction.OUTBOUND:
            return self._progress.bisect_right(outbound_progress)
        return self._progress.bisect_left(outbound_progress) - 1

    def _first_ahead(self, direction: Direction, outbound_progress: float) -> Node[RouteStop] | None:
        """First stop strictly ahead of the bus (O(log n))."""
        index = self._first_ahead_index(direction, outbound_progress)
        return self._nodes[index] if 0 <= index < len(self._nodes) else None

    def stops_ahead(self, direction: Direction, outbound_progress: float, limit: int) -> list[RouteStop]:
        """Up to ``limit`` stops ahead of the bus, nearest first (walks the list: O(limit))."""
        result: list[RouteStop] = []
        node = self._first_ahead(direction, outbound_progress)
        while node is not None and len(result) < limit:
            result.append(node.value)
            node = self._step(node, direction)
        return result

    def stops_remaining(self, direction: Direction, outbound_progress: float, stop_id: str) -> int:
        """Number of stops the bus still has to reach ``stop_id`` (inclusive), or -1 if the bus
        has already passed it. O(log n)."""
        target = self._by_id.get(stop_id)
        if target is None:
            raise NotFoundError("stop_not_in_route", params={"route_id": self.route_id, "stop_id": stop_id})
        target_index = target.value.order - 1  # list position == order in the route
        first = self._first_ahead_index(direction, outbound_progress)
        if direction is Direction.OUTBOUND:
            return target_index - first + 1 if first <= target_index else -1
        return first - target_index + 1 if target_index <= first else -1

    def distance_to_stop_m(self, direction: Direction, outbound_progress: float, stop_id: str) -> float:
        """Metres along the path to ``stop_id`` (0 when the bus already passed it)."""
        stop = self.require_stop(stop_id)
        ahead = self.travel_progress(direction, stop.progress) - self.travel_progress(direction, outbound_progress)
        return max(0.0, ahead) * self.path.length_m

    def next_stop(self, direction: Direction, stop_id: str) -> RouteStop | None:
        """Following stop in the given travel direction (O(1))."""
        node = self._by_id.get(stop_id)
        if node is None:
            return None
        following = self._step(node, direction)
        return None if following is None else following.value

    def distance_from_terminal_m(self, outbound_progress: float) -> float:
        """Distance to the nearest end of the path in metres."""
        return min(outbound_progress, 1.0 - outbound_progress) * self.path.length_m
