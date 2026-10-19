"""Occupancy engine: seat map, formula, door/camera fusion and reading history."""

from core_service.occupancy.count_fusion import CountFusion, FusionDecision, FusionReason
from core_service.occupancy.occupancy_calculator import OccupancyCalculator, round_half_up
from core_service.occupancy.reading_history import HistoryEntry, ReadingHistory, ReadingHistoryStore
from core_service.occupancy.seat_map import Overflow, Position, PositionKind, SeatMap

__all__ = [
    "CountFusion",
    "FusionDecision",
    "FusionReason",
    "HistoryEntry",
    "OccupancyCalculator",
    "Overflow",
    "Position",
    "PositionKind",
    "ReadingHistory",
    "ReadingHistoryStore",
    "SeatMap",
    "round_half_up",
]
