"""On-screen alert: what the passenger's phone shows (banner, lock-screen notification, inbox item).

It carries everything a client needs to be *usable*: plain-language text, severity and
priority, a vibration pattern (so the alert can be felt without looking at the phone), a
screen-reader label, a deep link and a TTL. Colour is never the only carrier of meaning.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class ScreenAlertKind(str, Enum):
    PROXIMITY = "proximity"
    OCCUPANCY_WARNING = "occupancy_warning"
    BUS_LOST = "bus_lost"
    ALERT_EXPIRED = "alert_expired"


class AlertSeverity(str, Enum):
    INFO = "info"
    WARNING = "warning"


class AlertPriority(str, Enum):
    NORMAL = "normal"
    HIGH = "high"


@dataclass(frozen=True)
class ScreenAlert:
    notification_id: str
    kind: ScreenAlertKind
    severity: AlertSeverity
    priority: AlertPriority
    title: str
    body: str
    accessibility_label: str
    haptic_pattern: tuple[int, ...]  # milliseconds: pause, vibrate, pause, vibrate, ...
    sound: str
    channel_id: str
    deep_link: str
    ttl_seconds: int
    dedupe_key: str
    locale: str
    created_at: float
    data: Mapping[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "notification_id": self.notification_id,
            "kind": self.kind.value,
            "severity": self.severity.value,
            "priority": self.priority.value,
            "title": self.title,
            "body": self.body,
            "accessibility_label": self.accessibility_label,
            "haptic_pattern": list(self.haptic_pattern),
            "sound": self.sound,
            "channel_id": self.channel_id,
            "deep_link": self.deep_link,
            "ttl_seconds": self.ttl_seconds,
            "dedupe_key": self.dedupe_key,
            "locale": self.locale,
            "created_at": self.created_at,
            "data": dict(self.data),
        }

    @classmethod
    def from_dict(cls, document: Mapping[str, Any]) -> ScreenAlert:
        return cls(
            notification_id=document["notification_id"],
            kind=ScreenAlertKind(document["kind"]),
            severity=AlertSeverity(document["severity"]),
            priority=AlertPriority(document["priority"]),
            title=document["title"],
            body=document["body"],
            accessibility_label=document["accessibility_label"],
            haptic_pattern=tuple(int(v) for v in document["haptic_pattern"]),
            sound=document["sound"],
            channel_id=document["channel_id"],
            deep_link=document["deep_link"],
            ttl_seconds=int(document["ttl_seconds"]),
            dedupe_key=document["dedupe_key"],
            locale=document["locale"],
            created_at=float(document["created_at"]),
            data=dict(document.get("data", {})),
        )

    def push_data(self) -> dict[str, str]:
        """Flat string map for push payloads (FCM/APNs data messages carry strings only)."""
        payload = {str(k): str(v) for k, v in self.data.items()}
        payload.update(
            notification_id=self.notification_id,
            kind=self.kind.value,
            severity=self.severity.value,
            deep_link=self.deep_link,
            accessibility_label=self.accessibility_label,
        )
        return payload
