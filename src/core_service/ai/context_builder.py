"""Builds the compact, numeric context the AI receives.

Only fleet data goes in: occupancy, confidence and position of buses. No user, device or image
data exists in this context (and the :class:`PrivacyGuard` checks it anyway). The context is
bounded: buses are filtered by route and radius, ordered by occupancy and truncated.

With a :class:`FleetFeatureService` the context also carries the streaming statistics of each bus and the
bus spacing (headway) of each route, so the model reasons over indicators instead of raw rows.
"""

from __future__ import annotations

from typing import Any

from core_service.ai.features import FleetFeatureService, headway_report
from core_service.ai.privacy_guard import PrivacyGuard
from core_service.ai.schemas import AnalysisRequest
from core_service.clock import Clock
from core_service.data_structures import smallest_k
from core_service.models import Direction, LiveBusState, haversine_m
from core_service.persistence.relational import TelemetryRepository
from core_service.routing import RouteCatalog
from core_service.services.live_state_store import LiveStateStore

RECENT_SAMPLES = 5
RECENT_WINDOW_SECONDS = 3600.0
TREND_BUCKETS = 6  # 6 x 5 minutes = the last half hour
STALE_AFTER_SECONDS = 60.0


class ContextBuilder:
    def __init__(
        self,
        live_states: LiveStateStore,
        catalog: RouteCatalog,
        telemetry: TelemetryRepository | None,
        clock: Clock,
        guard: PrivacyGuard,
        max_buses: int = 60,
        features: FleetFeatureService | None = None,
    ) -> None:
        self._live_states = live_states
        self._catalog = catalog
        self._telemetry = telemetry
        self._clock = clock
        self._guard = guard
        self._max_buses = max_buses
        self._features = features

    def build(self, request: AnalysisRequest) -> dict[str, Any]:
        now = self._clock.now()
        route_ids = list(request.route_ids) or self._catalog.route_ids()
        by_route = self._live_states.list_by_routes(route_ids)  # one batched read for every route
        candidates: list[LiveBusState] = [state for route_id in route_ids for state in by_route.get(route_id, [])]
        if request.center is not None:
            radius_m = request.radius_km * 1000.0
            candidates = [s for s in candidates if haversine_m(request.center, _position(s)) <= radius_m]
        truncated = len(candidates) > self._max_buses
        # The fullest buses first; only ``max_buses`` are kept: bounded heap selection, O(n log k).
        selected = smallest_k(candidates, self._max_buses, key=lambda s: (-s.occupancy_pct, s.bus_id))

        recent = self._recent_occupancy([s.bus_id for s in selected], now)
        stats = {} if self._features is None else self._features.bus_features([s.bus_id for s in selected])
        routes: dict[str, list[dict[str, Any]]] = {}
        for state in selected:
            view = self._bus_view(state, now, recent.get(state.bus_id, []))
            if state.bus_id in stats:
                view["features"] = stats[state.bus_id].view(now)
            routes.setdefault(state.route_id or "-", []).append(view)
        context = {
            "generated_at": now,
            "query": PrivacyGuard.sanitize_text(request.query, 300),
            "scope": {"routes": route_ids, "radius_km": request.radius_km if request.center else None},
            "truncated": truncated,
            "routes": [
                {
                    "route_id": route_id,
                    "trend_5min": self._trend(route_id, now),
                    **self._headway(route_id, by_route.get(route_id)),
                    "buses": buses,
                }
                for route_id, buses in sorted(routes.items())
            ],
        }
        self._guard.assert_clean(context)
        return context

    def _headway(self, route_id: str, states: list[LiveBusState] | None) -> dict[str, Any]:
        """Bus spacing of the route (all its live buses, not only the selected ones)."""
        track = self._catalog.find_track(route_id) if route_id != "-" else None
        if self._features is None or track is None or not states:
            return {}
        now = self._clock.now()
        return {
            "headway": {
                direction.value: headway_report(track, states, direction, now, STALE_AFTER_SECONDS)
                for direction in (Direction.OUTBOUND, Direction.RETURN)
            }
        }

    def _recent_occupancy(self, bus_ids: list[str], now: float) -> dict[str, list[int]]:
        if self._telemetry is None:
            return {}
        return self._telemetry.recent_occupancy_many(bus_ids, RECENT_SAMPLES, now - RECENT_WINDOW_SECONDS)

    def _trend(self, route_id: str, now: float) -> list[dict[str, Any]]:
        """Occupancy of the whole route in 5-minute buckets (continuous aggregate), newest first."""
        if self._telemetry is None or route_id == "-":
            return []
        rows = self._telemetry.route_trend(route_id, now - TREND_BUCKETS * 300.0, TREND_BUCKETS)
        return [
            {
                "minutes_ago": round(max(0.0, now - float(row["bucket_ts"])) / 60.0),
                "avg_occupancy_pct": round(float(row["avg_occupancy_pct"] or 0.0), 1),
                "max_occupancy_pct": int(row["max_occupancy_pct"] or 0),
                "buses": int(row["buses"]),
            }
            for row in rows
        ]

    def _bus_view(self, state: LiveBusState, now: float, recent: list[int]) -> dict[str, Any]:
        return {
            "bus_id": state.bus_id,
            "direction": state.direction.value if state.direction else None,
            "occupancy_pct": state.occupancy_pct,
            "state": state.state.value,
            "people_on_board": state.people_on_board,
            "seated": state.seated,
            "standing": state.standing,
            "confidence": round(state.confidence, 3),
            "approximate_count": state.approximate_count,
            "off_route": state.off_route,
            "seconds_since_reading": round(max(0.0, now - state.reading_ts), 1),
            "recent_occupancy_pct": recent,
            "lat": round(state.lat, 5),
            "lng": round(state.lng, 5),
        }


def _position(state: LiveBusState) -> Any:
    from core_service.models import GeoPoint

    return GeoPoint(state.lat, state.lng)
