"""Results of the occupancy engine and the live state published to clients."""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any

from core_service.models.enums import Direction, OccupancyState


@dataclass(frozen=True)
class OccupancyResult:
    """Output of :class:`OccupancyCalculator` for one reading."""

    seated: int
    standing: int
    standing_out_of_position: int
    wheelchair: int
    people_on_board: int
    percentage: int
    state: OccupancyState
    free_seats: int
    free_standing: int
    wheelchair_free: bool
    confidence: float
    approximate_count: bool

    @property
    def occupants(self) -> int:
        """People that take up capacity (children in arms excluded)."""
        return self.seated + self.standing + self.standing_out_of_position + self.wheelchair


@dataclass(frozen=True)
class LiveBusState:
    """What passengers see for a bus right now."""

    bus_id: str
    route_id: str | None
    direction: Direction | None
    lat: float
    lng: float
    speed_kmh: float | None
    seated: int
    standing: int
    standing_out_of_position: int
    people_on_board: int
    occupancy_pct: int
    state: OccupancyState
    free_seats: int
    free_standing: int
    wheelchair_free: bool
    confidence: float
    approximate_count: bool
    outbound_progress: float | None
    off_route: bool
    model_version: str
    reading_ts: float
    updated_at: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "bus_id": self.bus_id,
            "route_id": self.route_id,
            "direction": self.direction.value if self.direction else None,
            "lat": self.lat,
            "lng": self.lng,
            "speed_kmh": self.speed_kmh,
            "seated": self.seated,
            "standing": self.standing,
            "standing_out_of_position": self.standing_out_of_position,
            "people_on_board": self.people_on_board,
            "occupancy_pct": self.occupancy_pct,
            "state": self.state.value,
            "free_seats": self.free_seats,
            "free_standing": self.free_standing,
            "wheelchair_free": self.wheelchair_free,
            "confidence": self.confidence,
            "approximate_count": self.approximate_count,
            "outbound_progress": self.outbound_progress,
            "off_route": self.off_route,
            "model_version": self.model_version,
            "reading_ts": self.reading_ts,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> LiveBusState:
        direction = data.get("direction")
        return cls(
            bus_id=data["bus_id"],
            route_id=data.get("route_id"),
            direction=Direction(direction) if direction else None,
            lat=data["lat"],
            lng=data["lng"],
            speed_kmh=data.get("speed_kmh"),
            seated=data["seated"],
            standing=data["standing"],
            standing_out_of_position=data["standing_out_of_position"],
            people_on_board=data["people_on_board"],
            occupancy_pct=data["occupancy_pct"],
            state=OccupancyState(data["state"]),
            free_seats=data["free_seats"],
            free_standing=data["free_standing"],
            wheelchair_free=data["wheelchair_free"],
            confidence=data["confidence"],
            approximate_count=data["approximate_count"],
            outbound_progress=data.get("outbound_progress"),
            off_route=data["off_route"],
            model_version=data["model_version"],
            reading_ts=data["reading_ts"],
            updated_at=data["updated_at"],
        )

    def as_no_data(self, now: float) -> LiveBusState:
        """Same bus, marked as not reporting (grey indicator in the app)."""
        return replace(self, state=OccupancyState.NO_DATA, updated_at=now)
