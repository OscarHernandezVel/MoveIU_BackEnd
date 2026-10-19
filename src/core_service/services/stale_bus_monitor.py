"""Detects buses that stopped reporting (RNF-8.3, story 4.1: 'Data not available')."""

from __future__ import annotations

from core_service.clock import Clock
from core_service.config import OccupancySettings
from core_service.metrics import MetricsRegistry
from core_service.models import OccupancyState
from core_service.services.event_bus import EventBus
from core_service.services.events import BusWentSilent
from core_service.services.live_state_store import LiveStateStore


class StaleBusMonitor:
    def __init__(
        self,
        live_states: LiveStateStore,
        events: EventBus,
        clock: Clock,
        settings: OccupancySettings,
        metrics: MetricsRegistry,
    ) -> None:
        self._live_states = live_states
        self._events = events
        self._clock = clock
        self._settings = settings
        self._metrics = metrics

    def check(self) -> int:
        """Mark silent buses as 'no data' and publish one event per bus; returns how many changed."""
        now = self._clock.now()
        changed = 0
        for state in self._live_states.list_all():
            if state.state is OccupancyState.NO_DATA:
                continue
            silent_for = now - state.reading_ts
            if silent_for <= self._settings.no_data_after_seconds:
                continue
            self._live_states.save(state.as_no_data(now))
            self._events.publish(BusWentSilent(state.bus_id, state.route_id, state.reading_ts, silent_for))
            self._metrics.increment("buses_went_silent")
            changed += 1
        return changed
