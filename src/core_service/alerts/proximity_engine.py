"""Proximity rule: is a bus close enough to the passenger's stop? (PB-07)"""

from __future__ import annotations

from dataclasses import dataclass

from core_service.models import LiveBusState, OccupancyState, ProximityAlert
from core_service.routing import RouteTrack


@dataclass(frozen=True)
class ProximityHit:
    stops_remaining: int
    distance_m: float


class ProximityEngine:
    """A hit means: same route and direction, the bus has not passed the stop yet, and it is
    ``stops_before`` stops away or closer, or within ``distance_threshold_m`` along the path."""

    def __init__(self, distance_threshold_m: float = 600.0) -> None:
        self._distance_threshold_m = distance_threshold_m

    def evaluate(self, alert: ProximityAlert, bus: LiveBusState, track: RouteTrack) -> ProximityHit | None:
        if bus.route_id != alert.route_id or bus.direction is not alert.direction:
            return None
        if alert.bus_id is not None and alert.bus_id != bus.bus_id:
            return None
        if bus.outbound_progress is None or bus.off_route or bus.state is OccupancyState.NO_DATA:
            return None
        if track.find_stop(alert.stop_id) is None:
            return None
        remaining = track.stops_remaining(alert.direction, bus.outbound_progress, alert.stop_id)
        if remaining < 0:
            return None  # already passed
        distance = track.distance_to_stop_m(alert.direction, bus.outbound_progress, alert.stop_id)
        if remaining <= alert.stops_before or distance <= self._distance_threshold_m:
            return ProximityHit(remaining, distance)
        return None
