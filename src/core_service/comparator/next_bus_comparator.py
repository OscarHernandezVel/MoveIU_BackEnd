"""'This bus or the next one' comparison (PB-04)."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from core_service.data_structures import Array, smallest_k
from core_service.models import Direction, LiveBusState, OccupancyState
from core_service.routing import RouteStop, RouteTrack


@dataclass(frozen=True)
class NextBus:
    state: LiveBusState
    stops_remaining: int
    distance_m: int
    recommended: bool


@dataclass(frozen=True)
class NextBusHint:
    """Compact description of the following bus, embedded in alert texts."""

    bus_id: str
    state: OccupancyState
    stops_remaining: int


class NextBusComparator:
    """Pure algorithm: filter, select the nearest ``limit`` buses (bounded heap), mark the recommended bus."""

    def compare(
        self,
        candidates: Iterable[LiveBusState],
        track: RouteTrack,
        direction: Direction,
        stop: RouteStop,
        limit: int = 2,
    ) -> list[NextBus]:
        approaching: Array[tuple[LiveBusState, int]] = Array()
        for state in candidates:
            if not self._is_usable(state, track, direction):
                continue
            assert state.outbound_progress is not None
            remaining = track.stops_remaining(direction, state.outbound_progress, stop.stop.stop_id)
            if remaining >= 1:  # -1 means the bus already passed the stop
                approaching.append((state, remaining))

        # Only the ``limit`` nearest matter: bounded heap selection, O(n log k) instead of sorting all n.
        # Nearest = furthest along in its travel direction; ties go to the bus seen first.
        nearest_first = smallest_k(
            approaching,
            max(1, limit),
            key=lambda item: -track.travel_progress(direction, item[0].outbound_progress or 0.0),
        )
        result: list[NextBus] = []
        for state, remaining in nearest_first:
            assert state.outbound_progress is not None
            distance = track.distance_to_stop_m(direction, state.outbound_progress, stop.stop.stop_id)
            result.append(NextBus(state, remaining, round(distance), False))

        if (
            len(result) >= 2
            and result[0].state.state.is_crowded
            and result[1].state.state is OccupancyState.AVAILABLE_SEATS
        ):
            second = result[1]
            result[1] = NextBus(second.state, second.stops_remaining, second.distance_m, True)
        return result

    @staticmethod
    def _is_usable(state: LiveBusState, track: RouteTrack, direction: Direction) -> bool:
        return (
            state.route_id == track.route_id
            and state.direction is direction
            and state.outbound_progress is not None
            and not state.off_route
            and state.state is not OccupancyState.NO_DATA
        )

    @staticmethod
    def hint_from(buses: list[NextBus], excluding_bus_id: str) -> NextBusHint | None:
        for item in buses:
            if item.state.bus_id != excluding_bus_id:
                return NextBusHint(item.state.bus_id, item.state.state, item.stops_remaining)
        return None
