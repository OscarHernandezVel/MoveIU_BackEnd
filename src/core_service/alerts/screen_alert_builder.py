"""Builds localised :class:`ScreenAlert` objects from domain facts."""

from __future__ import annotations

import uuid
from urllib.parse import urlencode

from core_service.alerts.proximity_engine import ProximityHit
from core_service.alerts.screen_alert import AlertPriority, AlertSeverity, ScreenAlert, ScreenAlertKind
from core_service.clock import Clock
from core_service.comparator import NextBusHint
from core_service.i18n import DEFAULT_LOCALE, MessageCatalog
from core_service.models import LiveBusState, OccupancyState, ProximityAlert

CHANNEL_ID = "bus_alerts"
SOUND = "bus_arriving"
PUSH_TTL_SECONDS = 300

#: Vibration patterns in ms. Long and steady for "your bus is coming", triple pulse for warnings:
#: distinguishable by touch alone, so the passenger does not need to take the phone out.
HAPTIC_INFO = (0, 700, 250, 700)
HAPTIC_WARNING = (0, 400, 150, 400, 150, 400)
HAPTIC_SHORT = (0, 300)


class ScreenAlertBuilder:
    def __init__(self, catalog: MessageCatalog, clock: Clock) -> None:
        self._catalog = catalog
        self._clock = clock

    # ------------------------------------------------------------------ helpers
    def _state_label(self, state: OccupancyState, locale: str) -> str:
        return self._catalog.render(f"occupancy.state.{state.value}", locale)

    @staticmethod
    def _deep_link(bus_id: str, route_id: str, stop_id: str) -> str:
        return "capacidadbus://bus/" + bus_id + "?" + urlencode({"route": route_id, "stop": stop_id})

    def _base(
        self,
        *,
        kind: ScreenAlertKind,
        severity: AlertSeverity,
        priority: AlertPriority,
        title: str,
        body: str,
        a11y: str,
        haptic: tuple[int, ...],
        deep_link: str,
        dedupe_key: str,
        locale: str,
        data: dict[str, str],
    ) -> ScreenAlert:
        return ScreenAlert(
            notification_id=uuid.uuid4().hex,
            kind=kind,
            severity=severity,
            priority=priority,
            title=title,
            body=body,
            accessibility_label=a11y,
            haptic_pattern=haptic,
            sound=SOUND,
            channel_id=CHANNEL_ID,
            deep_link=deep_link,
            ttl_seconds=PUSH_TTL_SECONDS,
            dedupe_key=dedupe_key,
            locale=locale,
            created_at=self._clock.now(),
            data=data,
        )

    def _distance_text(self, stops: int, locale: str) -> str:
        key = "alert.proximity.distance.one" if stops <= 1 else "alert.proximity.distance.many"
        return self._catalog.render(key, locale, stops=stops)

    # ------------------------------------------------------------------ builders
    def proximity(
        self,
        alert: ProximityAlert,
        state: LiveBusState,
        hit: ProximityHit,
        stop_name: str,
        next_bus: NextBusHint | None = None,
        locale: str = DEFAULT_LOCALE,
    ) -> ScreenAlert:
        """'Your bus is N stops away', with its occupancy and the state of the next bus."""
        label = self._state_label(state.state, locale)
        distance = self._distance_text(hit.stops_remaining, locale)
        params = {
            "route_id": alert.route_id,
            "bus_id": state.bus_id,
            "stop_name": stop_name,
            "state_label": label,
            "occupancy_pct": state.occupancy_pct,
            "distance": distance,
        }
        body = self._catalog.render("alert.proximity.body", locale, **params)
        data = {
            "alert_id": alert.alert_id,
            "route_id": alert.route_id,
            "bus_id": state.bus_id,
            "stop_id": alert.stop_id,
            "stops_remaining": str(hit.stops_remaining),
            "distance_m": str(round(hit.distance_m)),
            "occupancy_state": state.state.value,
            "occupancy_pct": str(state.occupancy_pct),
        }
        if next_bus is not None:
            body += self._catalog.render(
                "alert.proximity.next_bus",
                locale,
                state_label=self._state_label(next_bus.state, locale).lower(),
                stops=next_bus.stops_remaining,
            )
            data["next_bus_id"] = next_bus.bus_id
            data["next_bus_state"] = next_bus.state.value
        crowded = state.state.is_crowded
        return self._base(
            kind=ScreenAlertKind.PROXIMITY,
            severity=AlertSeverity.WARNING if crowded else AlertSeverity.INFO,
            priority=AlertPriority.HIGH,
            title=self._catalog.render("alert.proximity.title", locale, **params),
            body=body,
            a11y=self._catalog.render("alert.proximity.a11y", locale, **params),
            haptic=HAPTIC_WARNING if crowded else HAPTIC_INFO,
            deep_link=self._deep_link(state.bus_id, alert.route_id, alert.stop_id),
            dedupe_key=f"proximity:{alert.alert_id}",
            locale=locale,
            data=data,
        )

    def occupancy_warning(
        self,
        alert: ProximityAlert,
        state: LiveBusState,
        next_bus: NextBusHint | None = None,
        locale: str = DEFAULT_LOCALE,
    ) -> ScreenAlert:
        """The bus the passenger is waiting for just became full."""
        params = {
            "route_id": alert.route_id,
            "bus_id": state.bus_id,
            "state_label": self._state_label(state.state, locale),
            "occupancy_pct": state.occupancy_pct,
        }
        body = self._catalog.render("alert.occupancy_warning.body", locale, **params)
        data = {"alert_id": alert.alert_id, "bus_id": state.bus_id, "occupancy_state": state.state.value}
        if next_bus is not None and next_bus.state is OccupancyState.AVAILABLE_SEATS:
            body += self._catalog.render("alert.occupancy_warning.alternative", locale, next_bus_id=next_bus.bus_id)
            data["next_bus_id"] = next_bus.bus_id
        return self._base(
            kind=ScreenAlertKind.OCCUPANCY_WARNING,
            severity=AlertSeverity.WARNING,
            priority=AlertPriority.HIGH,
            title=self._catalog.render("alert.occupancy_warning.title", locale, **params),
            body=body,
            a11y=self._catalog.render("alert.occupancy_warning.a11y", locale, **params),
            haptic=HAPTIC_WARNING,
            deep_link=self._deep_link(state.bus_id, alert.route_id, alert.stop_id),
            dedupe_key=f"occupancy_warning:{alert.alert_id}:{state.bus_id}:{state.state.value}",
            locale=locale,
            data=data,
        )

    def bus_lost(
        self, alert: ProximityAlert, bus_id: str, silent_seconds: int, locale: str = DEFAULT_LOCALE
    ) -> ScreenAlert:
        params = {"bus_id": bus_id, "seconds": silent_seconds, "route_id": alert.route_id}
        return self._base(
            kind=ScreenAlertKind.BUS_LOST,
            severity=AlertSeverity.WARNING,
            priority=AlertPriority.NORMAL,
            title=self._catalog.render("alert.bus_lost.title", locale, **params),
            body=self._catalog.render("alert.bus_lost.body", locale, **params),
            a11y=self._catalog.render("alert.bus_lost.a11y", locale, **params),
            haptic=HAPTIC_SHORT,
            deep_link=self._deep_link(bus_id, alert.route_id, alert.stop_id),
            dedupe_key=f"bus_lost:{alert.alert_id}:{bus_id}",
            locale=locale,
            data={"alert_id": alert.alert_id, "bus_id": bus_id},
        )

    def alert_expired(self, alert: ProximityAlert, stop_name: str, locale: str = DEFAULT_LOCALE) -> ScreenAlert:
        minutes = max(1, round((alert.expires_at - alert.created_at) / 60))
        params = {"route_id": alert.route_id, "stop_name": stop_name, "minutes": minutes}
        return self._base(
            kind=ScreenAlertKind.ALERT_EXPIRED,
            severity=AlertSeverity.INFO,
            priority=AlertPriority.NORMAL,
            title=self._catalog.render("alert.expired.title", locale, **params),
            body=self._catalog.render("alert.expired.body", locale, **params),
            a11y=self._catalog.render("alert.expired.a11y", locale, **params),
            haptic=HAPTIC_SHORT,
            deep_link="capacidadbus://alerts?" + urlencode({"route": alert.route_id, "stop": alert.stop_id}),
            dedupe_key=f"alert_expired:{alert.alert_id}",
            locale=locale,
            data={"alert_id": alert.alert_id, "route_id": alert.route_id},
        )
