"""Enumerations shared by the whole domain."""

from __future__ import annotations

from enum import Enum


class Direction(str, Enum):
    """Travel direction along a route. One stop list serves both directions:
    ``OUTBOUND`` walks it forwards (``next``), ``RETURN`` walks it backwards (``prev``)."""

    OUTBOUND = "outbound"
    RETURN = "return"


class OccupancyState(str, Enum):
    AVAILABLE_SEATS = "available_seats"
    STANDING_ONLY = "standing_only"
    FULL = "full"
    OVERCROWDED = "overcrowded"
    NO_DATA = "no_data"

    @property
    def is_crowded(self) -> bool:
        """True when a passenger should expect no free seat."""
        return self in (OccupancyState.STANDING_ONLY, OccupancyState.FULL, OccupancyState.OVERCROWDED)

    @property
    def icon(self) -> str:
        """Icon name for the UI. The state is never conveyed by colour alone."""
        return {
            OccupancyState.AVAILABLE_SEATS: "seat",
            OccupancyState.STANDING_ONLY: "standing",
            OccupancyState.FULL: "full",
            OccupancyState.OVERCROWDED: "warning",
            OccupancyState.NO_DATA: "offline",
        }[self]


class BusStatus(str, Enum):
    ACTIVE = "active"
    INACTIVE = "inactive"
    MAINTENANCE = "maintenance"


class Role(str, Enum):
    GUEST = "guest"
    PASSENGER = "passenger"
    AUDITOR = "auditor"
    ADMIN = "admin"


class AlertStatus(str, Enum):
    ACTIVE = "active"
    SENT = "sent"
    EXPIRED = "expired"
    CANCELLED = "cancelled"


class FindingSeverity(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class FindingKind(str, Enum):
    OVERCROWDING = "overcrowding"
    SILENT_BUS = "silent_bus"
    LOW_CONFIDENCE = "low_confidence"
    COUNT_MISMATCH = "count_mismatch"
    AI_RECOMMENDATION = "ai_recommendation"
