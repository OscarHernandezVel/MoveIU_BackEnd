"""Application service around the comparator: reads live states and applies the algorithm."""

from __future__ import annotations

from dataclasses import dataclass

from core_service.comparator.next_bus_comparator import NextBus, NextBusComparator, NextBusHint
from core_service.errors import NotFoundError
from core_service.models import Direction, LiveBusState
from core_service.routing import RouteCatalog
from core_service.services.live_state_store import LiveStateStore


@dataclass(frozen=True)
class NextBusesResult:
    stop_id: str
    stop_name: str
    route_id: str
    direction: Direction
    buses: list[NextBus]


class NextBusService:
    def __init__(
        self, catalog: RouteCatalog, live_states: LiveStateStore, comparator: NextBusComparator | None = None
    ) -> None:
        self._catalog = catalog
        self._live_states = live_states
        self._comparator = comparator or NextBusComparator()

    def get_next_buses(self, stop_id: str, route_id: str, direction: Direction, limit: int = 2) -> NextBusesResult:
        track = self._catalog.require_track(route_id)
        stop = track.find_stop(stop_id)
        if stop is None:
            raise NotFoundError("stop_not_in_route", params={"route_id": route_id, "stop_id": stop_id})
        buses = self._comparator.compare(
            self._live_states.list_by_route(route_id), track, direction, stop, limit=min(max(limit, 1), 3)
        )
        return NextBusesResult(stop.stop.stop_id, stop.stop.name, route_id, direction, buses)

    def hint_after(self, state: LiveBusState, stop_id: str) -> NextBusHint | None:
        """State of the bus that follows ``state`` towards ``stop_id`` (for alert texts)."""
        if state.route_id is None or state.direction is None:
            return None
        try:
            result = self.get_next_buses(stop_id, state.route_id, state.direction, limit=3)
        except NotFoundError:
            return None
        return self._comparator.hint_from(result.buses, excluding_bus_id=state.bus_id)
