"""Proximity alerts and on-screen notifications (PB-07, CU-05).

Flow for one bus update (subscribed to ``OccupancyUpdated``):

1. Take the stops the bus still has ahead (linked-list walk, binary-searched start).
2. For each stop, read the alerts waiting there from the O(1)-removal index.
3. Evaluate the proximity rule; require several consecutive hits (GPS jitter).
4. Deliver **once**: compare-and-set the alert to ``SENT`` in SQL, take it out of the index,
   dispatch to the channels. If dispatching fails, a LIFO saga puts everything back so the
   next reading can retry.
5. If the bus just became full, warn the waiting passengers (rate limited and de-duplicated).
"""

from __future__ import annotations

import logging
import uuid
from functools import partial

from core_service.alerts.alert_index import AlertIndex
from core_service.alerts.channels import DeviceTarget
from core_service.alerts.dispatcher import AlertDispatcher
from core_service.alerts.hit_counter import ConsecutiveHitCounter
from core_service.alerts.index_sync import IndexSync, NullIndexSync
from core_service.alerts.proximity_engine import ProximityEngine, ProximityHit
from core_service.alerts.screen_alert_builder import ScreenAlertBuilder
from core_service.alerts.throttle import Throttle
from core_service.clock import Clock
from core_service.comparator.next_bus_service import NextBusService
from core_service.config import AlertSettings
from core_service.errors import NotFoundError, ValidationError
from core_service.metrics import MetricsRegistry
from core_service.models import AlertStatus, Direction, LiveBusState, OccupancyState, ProximityAlert
from core_service.persistence.relational import AlertRepository, DeviceRepository
from core_service.routing import RouteCatalog, RouteStop, RouteTrack
from core_service.services.events import BusWentSilent, OccupancyUpdated
from core_service.services.saga import Saga

log = logging.getLogger(__name__)

_CROWDED_FOR_WARNING = (OccupancyState.FULL, OccupancyState.OVERCROWDED)


class AlertService:
    def __init__(
        self,
        *,
        alerts: AlertRepository,
        devices: DeviceRepository,
        catalog: RouteCatalog,
        index: AlertIndex,
        engine: ProximityEngine,
        hits: ConsecutiveHitCounter,
        builder: ScreenAlertBuilder,
        dispatcher: AlertDispatcher,
        throttle: Throttle,
        next_buses: NextBusService,
        clock: Clock,
        settings: AlertSettings,
        metrics: MetricsRegistry,
        index_sync: IndexSync | None = None,
    ) -> None:
        self._alerts = alerts
        self._devices = devices
        self._catalog = catalog
        self._index = index
        self._engine = engine
        self._hits = hits
        self._builder = builder
        self._dispatcher = dispatcher
        self._throttle = throttle
        self._next_buses = next_buses
        self._clock = clock
        self._settings = settings
        self._metrics = metrics
        self._sync: IndexSync = index_sync or NullIndexSync()

    # ---------------------------------------------------------------- index (local + other replicas)
    def _index_add(self, alert: ProximityAlert) -> None:
        self._index.add(alert)
        self._sync.announce_added(alert)

    def _index_remove(self, alert_id: str) -> None:
        self._index.remove(alert_id)
        self._sync.announce_removed(alert_id)

    # ---------------------------------------------------------------- lifecycle
    def create(
        self,
        device_id: str,
        route_id: str,
        direction: Direction,
        stop_id: str,
        stops_before: int = 2,
        bus_id: str | None = None,
    ) -> ProximityAlert:
        if not 1 <= stops_before <= 5:
            raise ValidationError("validation_error", details=["stops_before must be between 1 and 5"])
        if self._devices.find(device_id) is None:
            raise NotFoundError("device_not_registered")
        track = self._catalog.require_track(route_id)
        if track.find_stop(stop_id) is None:
            raise NotFoundError("stop_not_in_route", params={"route_id": route_id, "stop_id": stop_id})
        if bus_id is not None:
            bus = self._catalog.require_bus(bus_id)
            if bus.route_id != route_id:
                raise ValidationError("bus_not_on_route", params={"bus_id": bus_id, "route_id": route_id})
        now = self._clock.now()
        if self._alerts.count_active(device_id, now) >= self._settings.max_active_per_device:
            raise ValidationError("alert_limit_reached")

        alert = ProximityAlert(
            alert_id=str(uuid.uuid4()),
            device_id=device_id,
            route_id=route_id,
            direction=direction,
            stop_id=stop_id,
            stops_before=stops_before,
            created_at=now,
            expires_at=now + self._settings.expiration_seconds,
            bus_id=bus_id,
        )
        self._alerts.create(alert)
        self._index_add(alert)
        self._metrics.increment("alerts_created")
        return alert

    def list_active(self, device_id: str) -> list[ProximityAlert]:
        return self._alerts.list_active_for_device(device_id, self._clock.now())

    def cancel(self, device_id: str, alert_id: str) -> None:
        cancelled = self._alerts.cancel(alert_id, device_id)
        if cancelled is None:
            raise NotFoundError("alert_not_found")
        self._index_remove(alert_id)
        self._hits.forget(alert_id)

    def rebuild_index(self) -> int:
        """Reload active alerts from SQL (start-up, or after the in-memory index was lost)."""
        active = self._alerts.list_all_active(self._clock.now())
        self._index.replace_all(active)  # one atomic swap: readers never see a half-empty index
        return len(active)

    def expire_due(self) -> int:
        """Expire alerts whose time is over and tell the passenger (scheduled job)."""
        expired = self._alerts.expire_due(self._clock.now())
        for alert in expired:
            self._index_remove(alert.alert_id)
            self._hits.forget(alert.alert_id)
            try:
                target = self._target(alert.device_id)
                if target is not None:
                    track = self._catalog.find_track(alert.route_id)
                    stop = track.find_stop(alert.stop_id) if track else None
                    name = stop.stop.name if stop else alert.stop_id
                    self._dispatcher.dispatch(target, self._builder.alert_expired(alert, name, target.locale))
            except Exception:  # noqa: BLE001 - expiry bookkeeping already succeeded
                log.exception("Could not notify the expiry of alert %s", alert.alert_id)
        self._hits.sweep()
        self._throttle.sweep()
        return len(expired)

    # ------------------------------------------------------------ event handlers
    def on_occupancy_updated(self, event: OccupancyUpdated) -> None:
        state = event.state
        track = self._catalog.find_track(state.route_id)
        if (
            track is None
            or state.direction is None
            or state.outbound_progress is None
            or state.off_route
            or state.state is OccupancyState.NO_DATA
        ):
            return
        became_full = state.state in _CROWDED_FOR_WARNING and not self._was_evaluated_as_crowded(event.previous)
        for route_stop in track.stops_ahead(state.direction, state.outbound_progress, self._settings.max_stops_ahead):
            for alert in self._index.for_stop(route_stop.stop.stop_id):
                if alert.route_id != state.route_id or alert.direction is not state.direction:
                    continue
                if alert.bus_id not in (None, state.bus_id):
                    continue
                try:
                    self._handle_alert(alert, state, track, route_stop, became_full)
                except Exception:  # noqa: BLE001 - one failing device must not block the others
                    self._metrics.increment("alerts_handler_errors")
                    log.exception("Alert %s could not be processed", alert.alert_id)

    @staticmethod
    def _was_evaluated_as_crowded(previous: LiveBusState | None) -> bool:
        """A previous state only counts when alerts could have looked at it (direction and position known)."""
        return (
            previous is not None
            and previous.state in _CROWDED_FOR_WARNING
            and previous.direction is not None
            and previous.outbound_progress is not None
            and not previous.off_route
        )

    def on_bus_silent(self, event: BusWentSilent) -> None:
        for alert in self._index.all():
            if alert.bus_id != event.bus_id:
                continue
            try:
                target = self._target(alert.device_id)
                if target is not None:
                    screen = self._builder.bus_lost(alert, event.bus_id, round(event.silent_for_seconds), target.locale)
                    admit = partial(self._throttle.allow, alert.device_id)
                    self._dispatcher.dispatch(target, screen, admit=admit)
            except Exception:  # noqa: BLE001
                log.exception("Could not send the signal-lost notice of alert %s", alert.alert_id)

    # ---------------------------------------------------------------- internals
    def _handle_alert(
        self, alert: ProximityAlert, state: LiveBusState, track: RouteTrack, route_stop: RouteStop, became_full: bool
    ) -> None:
        hit = self._engine.evaluate(alert, state, track)
        if hit is None:
            self._hits.reset(alert.alert_id, state.bus_id)
        else:
            streak = self._hits.hit(alert.alert_id, state.bus_id)
            if streak >= self._settings.consecutive_hits:
                self._deliver_proximity(alert, state, hit, route_stop)
        if became_full and self._index.get(alert.alert_id) is not None:
            self._warn_crowded(alert, state)

    def _target(self, device_id: str) -> DeviceTarget | None:
        device = self._devices.find(device_id)
        if device is None:
            return None
        return DeviceTarget(device.device_id, device.push_token, device.platform)

    def _deliver_proximity(
        self, alert: ProximityAlert, state: LiveBusState, hit: ProximityHit, route_stop: RouteStop
    ) -> None:
        target = self._target(alert.device_id)
        if target is None:
            self._index_remove(alert.alert_id)
            return
        next_bus = self._next_buses.hint_after(state, alert.stop_id)
        screen = self._builder.proximity(alert, state, hit, route_stop.stop.name, next_bus, target.locale)

        if not self._alerts.transition(alert.alert_id, AlertStatus.ACTIVE, AlertStatus.SENT, sent_at=self._clock.now()):
            self._index_remove(alert.alert_id)  # already delivered, cancelled or expired elsewhere
            return
        with Saga() as saga:
            saga.on_rollback(lambda: self._alerts.transition(alert.alert_id, AlertStatus.SENT, AlertStatus.ACTIVE))
            saga.step(lambda: self._index_remove(alert.alert_id), compensate=lambda: self._index_add(alert))
            saga.step(lambda: self._dispatcher.dispatch(target, screen))
        self._hits.forget(alert.alert_id)
        self._metrics.increment("alerts_proximity_sent")

    def _warn_crowded(self, alert: ProximityAlert, state: LiveBusState) -> None:
        target = self._target(alert.device_id)
        if target is None:
            return
        next_bus = self._next_buses.hint_after(state, alert.stop_id)
        screen = self._builder.occupancy_warning(alert, state, next_bus, target.locale)
        self._dispatcher.dispatch(
            target,
            screen,
            dedupe_ttl_seconds=self._settings.warning_dedupe_seconds,
            admit=lambda: self._throttle.allow(alert.device_id),
        )
