"""In-memory catalog of routes and buses, replaced atomically on every reload."""

from __future__ import annotations

import threading
from collections.abc import Iterable

from core_service.errors import NotFoundError
from core_service.models import Bus
from core_service.routing.route_track import RouteTrack


class RouteCatalog:
    """Read-mostly registry. ``replace_all`` swaps the whole content under a lock so
    concurrent readers always see a consistent snapshot."""

    def __init__(self) -> None:
        self._tracks: dict[str, RouteTrack] = {}
        self._buses: dict[str, Bus] = {}
        self._lock = threading.Lock()

    def replace_all(self, tracks: Iterable[RouteTrack], buses: Iterable[Bus]) -> None:
        new_tracks = {track.route_id: track for track in tracks}
        new_buses = {bus.bus_id: bus for bus in buses}
        with self._lock:
            self._tracks = new_tracks
            self._buses = new_buses

    def route_ids(self) -> list[str]:
        with self._lock:
            return sorted(self._tracks)

    def tracks(self) -> list[RouteTrack]:
        with self._lock:
            return [self._tracks[key] for key in sorted(self._tracks)]

    def find_track(self, route_id: str | None) -> RouteTrack | None:
        if route_id is None:
            return None
        with self._lock:
            return self._tracks.get(route_id)

    def require_track(self, route_id: str) -> RouteTrack:
        track = self.find_track(route_id)
        if track is None:
            raise NotFoundError("route_not_found", params={"route_id": route_id})
        return track

    def find_bus(self, bus_id: str) -> Bus | None:
        with self._lock:
            return self._buses.get(bus_id)

    def require_bus(self, bus_id: str) -> Bus:
        bus = self.find_bus(bus_id)
        if bus is None:
            raise NotFoundError("bus_not_found", params={"bus_id": bus_id})
        return bus

    def buses(self) -> list[Bus]:
        with self._lock:
            return [self._buses[key] for key in sorted(self._buses)]
