"""Live state of every bus, kept in the document store with a TTL.

The route index (route -> bus ids) is a *set stored in the document store* (a Redis ``SET`` in
production), not a dictionary of this process: every replica sees the same index, so listing a
route never scans every document and never depends on which replica handled the last reading.
Stale members (a bus whose document expired) are removed lazily when the route is listed.
"""

from __future__ import annotations

from core_service.clock import Clock
from core_service.models import LiveBusState
from core_service.persistence.documents import DocumentStore

COLLECTION = "live_state"
ROUTE_INDEX = "live_route_index"


class LiveStateStore:
    def __init__(self, store: DocumentStore, clock: Clock, ttl_seconds: float = 600.0) -> None:
        self._store = store
        self._clock = clock
        self._ttl = ttl_seconds
        self.rebuild_index()

    def rebuild_index(self) -> None:
        """Recreate the route index from the persisted documents (start-up, or after the index was lost).

        Only adds members (``SADD`` is idempotent), so several replicas can run it at the same time."""
        for document in self._store.find(COLLECTION):
            route_id = document.get("route_id")
            if route_id:
                self._store.add_to_set(ROUTE_INDEX, route_id, document["bus_id"])

    def save(self, state: LiveBusState) -> None:
        previous = self._store.get(COLLECTION, state.bus_id)
        self._store.put(COLLECTION, state.bus_id, state.to_dict(), ttl_seconds=self._ttl)
        old_route = previous.get("route_id") if previous else None
        if old_route and old_route != state.route_id:
            self._store.remove_from_set(ROUTE_INDEX, old_route, state.bus_id)
        if state.route_id:
            self._store.add_to_set(ROUTE_INDEX, state.route_id, state.bus_id)

    def get(self, bus_id: str) -> LiveBusState | None:
        document = self._store.get(COLLECTION, bus_id)
        return None if document is None else LiveBusState.from_dict(document)

    def delete(self, bus_id: str) -> None:
        previous = self._store.get(COLLECTION, bus_id)
        self._store.delete(COLLECTION, bus_id)
        if previous and previous.get("route_id"):
            self._store.remove_from_set(ROUTE_INDEX, previous["route_id"], bus_id)

    def list_by_route(self, route_id: str) -> list[LiveBusState]:
        """Live buses of a route: two round-trips in total (``SMEMBERS`` + one ``MGET``), whatever the fleet
        size; before, one ``GET`` per bus. It runs on every WebSocket tick and every comparator query."""
        return self.list_by_routes([route_id]).get(route_id, [])

    def list_by_routes(self, route_ids: list[str]) -> dict[str, list[LiveBusState]]:
        """Live buses of several routes with a single batched document read."""
        members = {route_id: sorted(self._store.set_members(ROUTE_INDEX, route_id)) for route_id in route_ids}
        wanted = sorted({bus_id for bus_ids in members.values() for bus_id in bus_ids})
        documents = self._store.get_many(COLLECTION, wanted)
        result: dict[str, list[LiveBusState]] = {}
        for route_id, bus_ids in members.items():
            states: list[LiveBusState] = []
            for bus_id in bus_ids:
                document = documents.get(bus_id)
                if document is None:  # expired: the bus stopped serving the route
                    self._store.remove_from_set(ROUTE_INDEX, route_id, bus_id)
                    continue
                state = LiveBusState.from_dict(document)
                if state.route_id == route_id:
                    states.append(state)
            result[route_id] = states
        return result

    def list_all(self) -> list[LiveBusState]:
        return [LiveBusState.from_dict(d) for d in self._store.find(COLLECTION)]

    def now(self) -> float:
        return self._clock.now()
