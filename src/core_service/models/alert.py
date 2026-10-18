"""Proximity alert requested by a passenger."""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any

from core_service.models.enums import AlertStatus, Direction


@dataclass(frozen=True)
class ProximityAlert:
    """ "Notify me when a bus of this route is ``stops_before`` stops from my stop"."""

    alert_id: str
    device_id: str
    route_id: str
    direction: Direction
    stop_id: str
    stops_before: int
    created_at: float
    expires_at: float
    bus_id: str | None = None
    status: AlertStatus = AlertStatus.ACTIVE
    sent_at: float | None = None

    def with_status(self, status: AlertStatus, sent_at: float | None = None) -> ProximityAlert:
        return replace(self, status=status, sent_at=sent_at if sent_at is not None else self.sent_at)

    def to_dict(self) -> dict[str, Any]:
        return {
            "alert_id": self.alert_id,
            "device_id": self.device_id,
            "route_id": self.route_id,
            "direction": self.direction.value,
            "stop_id": self.stop_id,
            "bus_id": self.bus_id,
            "stops_before": self.stops_before,
            "status": self.status.value,
            "created_at": self.created_at,
            "expires_at": self.expires_at,
            "sent_at": self.sent_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ProximityAlert:
        return cls(
            alert_id=data["alert_id"],
            device_id=data["device_id"],
            route_id=data["route_id"],
            direction=Direction(data["direction"]),
            stop_id=data["stop_id"],
            stops_before=int(data["stops_before"]),
            created_at=float(data["created_at"]),
            expires_at=float(data["expires_at"]),
            bus_id=data.get("bus_id"),
            status=AlertStatus(data.get("status", AlertStatus.ACTIVE.value)),
            sent_at=data.get("sent_at"),
        )
