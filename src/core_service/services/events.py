"""Domain events published on the in-process event bus."""

from __future__ import annotations

from dataclasses import dataclass

from core_service.models import LiveBusState, Reading
from core_service.occupancy.count_fusion import FusionReason


@dataclass(frozen=True)
class OccupancyUpdated:
    """A reading was processed and the live state of a bus changed."""

    state: LiveBusState
    previous: LiveBusState | None
    fusion_reason: FusionReason
    #: The reading behind the new state (door and camera counts feed the AI statistics).
    reading: Reading | None = None


@dataclass(frozen=True)
class BusWentSilent:
    """A bus stopped reporting for longer than the allowed time."""

    bus_id: str
    route_id: str | None
    last_reading_ts: float
    silent_for_seconds: float
