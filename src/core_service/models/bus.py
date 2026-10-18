"""Bus, capacity and stop entities."""

from __future__ import annotations

from dataclasses import dataclass

from core_service.errors import ValidationError
from core_service.models.enums import BusStatus
from core_service.models.geo import GeoPoint


@dataclass(frozen=True)
class BusCapacity:
    """Real capacity of a vehicle, from its interior layout.

    A *standing position* is the aisle space between two rows of seats and holds
    exactly one person. Door rows and the rear row of five seats add no standing
    positions. Door zones are transit areas and add no capacity either.
    """

    seats: int
    standing_positions: int
    wheelchair_spaces: int = 0
    door_zones: int = 2

    def __post_init__(self) -> None:
        if self.seats <= 0:
            raise ValidationError("invalid_capacity", details=["seats must be positive"])
        if self.standing_positions < 0 or self.wheelchair_spaces < 0 or self.door_zones < 0:
            raise ValidationError("invalid_capacity", details=["counts must not be negative"])

    @property
    def total(self) -> int:
        """Denominator of the occupancy formula (equals 100 %)."""
        return self.seats + self.standing_positions + self.wheelchair_spaces


@dataclass(frozen=True)
class Bus:
    bus_id: str
    route_id: str | None
    model: str
    capacity: BusCapacity
    status: BusStatus = BusStatus.ACTIVE


@dataclass(frozen=True)
class Stop:
    stop_id: str
    name: str
    location: GeoPoint
    active: bool = True
