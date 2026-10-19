"""Per-route feed: turns the live states of a route into snapshot and delta frames.

Algorithm (once per route per tick, however many clients follow the route):

1. project every live state onto the compact wire form (``compact_bus``; coordinates rounded to
   ~1 m, the state forced to ``no_data`` when the bus is silent, RNF-8.3);
2. compare with the previous tick through a hash map ``bus_id -> fingerprint``: new or changed buses go
   to ``upsert``, vanished ones to ``remove``. O(n) for n buses of the route;
3. serialise **once**: the same text is sent to every subscriber (fan-out cost O(1) per client instead
   of re-encoding per client). The snapshot text is built lazily, only if some client needs it.

``seq`` grows only when something changed, so an idle route costs nothing on the wire. A client that
joins receives the current snapshot and then the deltas that follow it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from core_service.models import LiveBusState, OccupancyState
from core_service.realtime.protocol import Channel, frame

Fingerprint = tuple[Any, ...]


def compact_bus(state: LiveBusState, now: float, stale_after_seconds: float) -> dict[str, Any]:
    """What the map needs for one bus (v7 payload: id, route, lat, lng, seated, standing, %)."""
    silent = state.state is OccupancyState.NO_DATA or now - state.reading_ts > stale_after_seconds
    return {
        "bus": state.bus_id,
        "dir": state.direction.value if state.direction else None,
        "lat": round(state.lat, 5),
        "lng": round(state.lng, 5),
        "pct": state.occupancy_pct,
        "state": OccupancyState.NO_DATA.value if silent else state.state.value,
        "seated": state.seated,
        "standing": state.standing,
        "free_seats": state.free_seats,
        "free_standing": state.free_standing,
        "wheelchair": state.wheelchair_free,
        "conf": round(state.confidence, 2),
        "approx": state.approximate_count,
        "off_route": state.off_route,
        "ts": round(state.reading_ts),
    }


def _fingerprint(bus: dict[str, Any]) -> Fingerprint:
    return tuple(bus.values())


@dataclass
class FeedUpdate:
    """Outcome of one tick for one route."""

    seq: int
    delta: str | None  # ``None`` when nothing changed since the previous tick


@dataclass
class RouteFeed:
    route_id: str
    stale_after_seconds: float
    seq: int = 0
    ts: float = 0.0
    _buses: dict[str, dict[str, Any]] = field(default_factory=dict)
    _prints: dict[str, Fingerprint] = field(default_factory=dict)
    _snapshot_text: str | None = None

    def advance(self, states: list[LiveBusState], now: float) -> FeedUpdate:
        buses = {s.bus_id: compact_bus(s, now, self.stale_after_seconds) for s in states if s.route_id == self.route_id}
        prints = {bus_id: _fingerprint(bus) for bus_id, bus in buses.items()}
        upsert = [buses[bus_id] for bus_id in sorted(buses) if self._prints.get(bus_id) != prints[bus_id]]
        remove = sorted(bus_id for bus_id in self._prints if bus_id not in buses)
        self._buses, self._prints, self.ts = buses, prints, now
        if not upsert and not remove:
            return FeedUpdate(self.seq, None)
        self.seq += 1
        self._snapshot_text = None
        text = frame(
            "delta",
            channel=Channel.FLEET.value,
            route=self.route_id,
            seq=self.seq,
            ts=round(now, 3),
            upsert=upsert,
            remove=remove,
        )
        return FeedUpdate(self.seq, text)

    def snapshot(self) -> str:
        """Full state of the route at ``seq`` (built once per change, shared by every joining client)."""
        if self._snapshot_text is None:
            self._snapshot_text = frame(
                "snapshot",
                channel=Channel.FLEET.value,
                route=self.route_id,
                seq=self.seq,
                ts=round(self.ts, 3),
                buses=[self._buses[bus_id] for bus_id in sorted(self._buses)],
            )
        return self._snapshot_text


@dataclass
class InsightsFeed:
    """Same idea for the staff channel: the route statistics, sent only when they change."""

    route_id: str
    seq: int = 0
    _last: str | None = None
    _text: str | None = None

    def advance(self, data: dict[str, Any], now: float) -> str | None:
        body = {k: v for k, v in data.items() if k != "generated_at"}  # the timestamp alone is not a change
        key = frame("x", data=body)
        if key == self._last:
            return None
        self._last = key
        self.seq += 1
        self._text = frame("insights", route=self.route_id, seq=self.seq, ts=round(now, 3), data=body)
        return self._text

    def current(self) -> str | None:
        return self._text
