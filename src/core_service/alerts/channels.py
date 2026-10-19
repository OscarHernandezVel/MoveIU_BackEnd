"""Delivery channels for on-screen alerts.

* :class:`PushChannel` reaches the phone even when the app is closed (lock-screen notification).
* :class:`InAppChannel` writes to the passenger's inbox, shown as a banner when the app is open.

Both are attempted for every alert, so losing the push provider never loses the alert.
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass

from core_service.alerts.screen_alert import AlertPriority, ScreenAlert
from core_service.alerts.screen_inbox import ScreenInbox
from core_service.i18n import DEFAULT_LOCALE

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class DeviceTarget:
    device_id: str
    push_token: str
    platform: str
    locale: str = DEFAULT_LOCALE


class ChannelDeliveryError(Exception):
    """A channel could not deliver. ``retryable`` tells the dispatcher whether trying again can help
    (a network timeout can; an unregistered push token cannot)."""

    def __init__(self, message: str, retryable: bool = True) -> None:
        super().__init__(message)
        self.retryable = retryable


class NotificationChannel(ABC):
    name: str

    @abstractmethod
    def deliver(self, target: DeviceTarget, alert: ScreenAlert) -> None:
        """Deliver ``alert`` or raise :class:`ChannelDeliveryError`."""


@dataclass(frozen=True)
class PushPayload:
    token: str
    platform: str
    title: str
    body: str
    data: dict[str, str]
    channel_id: str
    priority: str  # "high" | "normal"
    sound: str
    vibrate_pattern: tuple[int, ...]
    ttl_seconds: int


class PushGateway(ABC):
    """Adapter to the push provider (FCM/APNs)."""

    @abstractmethod
    def send(self, payload: PushPayload) -> None: ...


class LoggingPushGateway(PushGateway):
    """Development gateway: writes the notification to the log instead of sending it."""

    def send(self, payload: PushPayload) -> None:
        token = payload.token[:6] + "…" if len(payload.token) > 6 else payload.token
        log.info("PUSH [%s %s] %s | %s", payload.platform, token, payload.title, payload.body)


class PushChannel(NotificationChannel):
    name = "push"

    def __init__(self, gateway: PushGateway) -> None:
        self._gateway = gateway

    def deliver(self, target: DeviceTarget, alert: ScreenAlert) -> None:
        payload = PushPayload(
            token=target.push_token,
            platform=target.platform,
            title=alert.title,
            body=alert.body,
            data=alert.push_data(),
            channel_id=alert.channel_id,
            priority="high" if alert.priority is AlertPriority.HIGH else "normal",
            sound=alert.sound,
            vibrate_pattern=alert.haptic_pattern,
            ttl_seconds=alert.ttl_seconds,
        )
        self._gateway.send(payload)


class InAppChannel(NotificationChannel):
    name = "in_app"

    def __init__(self, inbox: ScreenInbox) -> None:
        self._inbox = inbox

    def deliver(self, target: DeviceTarget, alert: ScreenAlert) -> None:
        try:
            self._inbox.push(target.device_id, alert)
        except Exception as error:  # noqa: BLE001
            raise ChannelDeliveryError(f"inbox write failed: {error}") from error
