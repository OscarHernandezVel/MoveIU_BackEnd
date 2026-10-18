"""A telemetry reading produced by the on-board hardware (edge ML + door sensors + GPS)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from core_service.errors import ValidationError
from core_service.models.geo import GeoPoint


@dataclass(frozen=True)
class Reading:
    """Counts only: no image ever leaves the bus (privacy by design).

    ``doors_total`` is the running count from the door sensors. ``seated``,
    ``standing``, ``standing_out_of_position``, ``children_in_arms`` and
    ``wheelchair_occupied`` come from the on-board vision model.
    """

    bus_id: str
    ts: float
    location: GeoPoint
    seated: int
    standing: int
    doors_total: int
    confidence: float
    model_version: str
    standing_out_of_position: int = 0
    children_in_arms: int = 0
    wheelchair_occupied: int = 0
    approximate_count: bool = False
    speed_kmh: float | None = None
    received_at: float | None = None

    def __post_init__(self) -> None:
        if not self.bus_id:
            raise ValidationError("invalid_telemetry", details=["bus_id is required"])
        if not self.model_version:
            raise ValidationError("invalid_telemetry", details=["model_version is required"])
        counts = {
            "seated": self.seated,
            "standing": self.standing,
            "doors_total": self.doors_total,
            "standing_out_of_position": self.standing_out_of_position,
            "children_in_arms": self.children_in_arms,
            "wheelchair_occupied": self.wheelchair_occupied,
        }
        negatives = [name for name, value in counts.items() if value < 0]
        if negatives:
            raise ValidationError("invalid_telemetry", details=[f"{n} must not be negative" for n in negatives])
        if not 0.0 <= self.confidence <= 1.0:
            raise ValidationError("invalid_telemetry", details=["confidence must be between 0 and 1"])
        if self.speed_kmh is not None and self.speed_kmh < 0:
            raise ValidationError("invalid_telemetry", details=["speed_kmh must not be negative"])

    @property
    def camera_total(self) -> int:
        """People on board according to the cameras (children in arms are not counted)."""
        return self.seated + self.standing + self.standing_out_of_position + self.wheelchair_occupied

    def to_dict(self) -> dict[str, Any]:
        return {
            "bus_id": self.bus_id,
            "ts": self.ts,
            "lat": self.location.lat,
            "lng": self.location.lng,
            "seated": self.seated,
            "standing": self.standing,
            "standing_out_of_position": self.standing_out_of_position,
            "children_in_arms": self.children_in_arms,
            "wheelchair_occupied": self.wheelchair_occupied,
            "doors_total": self.doors_total,
            "confidence": self.confidence,
            "approximate_count": self.approximate_count,
            "model_version": self.model_version,
            "speed_kmh": self.speed_kmh,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Reading:
        """Inverse of :meth:`to_dict` (used to restore a reading history shared between replicas)."""
        return cls(
            bus_id=data["bus_id"],
            ts=float(data["ts"]),
            location=GeoPoint(float(data["lat"]), float(data["lng"])),
            seated=int(data["seated"]),
            standing=int(data["standing"]),
            doors_total=int(data["doors_total"]),
            confidence=float(data["confidence"]),
            model_version=data["model_version"],
            standing_out_of_position=int(data.get("standing_out_of_position", 0)),
            children_in_arms=int(data.get("children_in_arms", 0)),
            wheelchair_occupied=int(data.get("wheelchair_occupied", 0)),
            approximate_count=bool(data.get("approximate_count", False)),
            speed_kmh=data.get("speed_kmh"),
        )
