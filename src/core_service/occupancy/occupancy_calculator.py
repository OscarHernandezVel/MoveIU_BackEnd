"""Occupancy formula (PB-06).

.. code-block:: text

    occupancy % = (occupied seats + people standing) / (seats + standing positions
                  + wheelchair spaces) x 100

100 % means every seat and every standing position is taken; more than 100 % means
overcrowding. The denominator is the *real* capacity of each vehicle.
"""

from __future__ import annotations

import math

from core_service.models import BusCapacity, OccupancyResult, OccupancyState, Reading
from core_service.occupancy.seat_map import PositionKind, SeatMap


def round_half_up(value: float) -> int:
    """Round .5 upwards (Python's ``round`` rounds half to even, which surprises users)."""
    return int(math.floor(value + 0.5))


class OccupancyCalculator:
    """Pure function object: no I/O, no state. Runs in microseconds."""

    def calculate(self, capacity: BusCapacity, reading: Reading, use_door_count: bool = False) -> OccupancyResult:
        seat_map = SeatMap(capacity)
        if use_door_count:
            seated, standing, wheelchair, out_of_position = self._from_door_count(capacity, reading)
            seat_map.place(seated, standing, wheelchair)
        else:
            overflow = seat_map.place(reading.seated, reading.standing, reading.wheelchair_occupied)
            out_of_position = reading.standing_out_of_position + overflow.total

        seated = seat_map.occupied_count(PositionKind.SEAT)
        standing = seat_map.occupied_count(PositionKind.STANDING)
        wheelchair = seat_map.occupied_count(PositionKind.WHEELCHAIR)
        occupants = seated + standing + wheelchair + out_of_position

        percentage = round_half_up(occupants * 100 / capacity.total)
        free_seats = seat_map.free_count(PositionKind.SEAT)
        state = self._classify(free_seats, occupants, capacity.total, out_of_position)

        approximate = use_door_count or reading.approximate_count
        confidence = min(reading.confidence, DOOR_COUNT_CONFIDENCE) if use_door_count else reading.confidence
        return OccupancyResult(
            seated=seated,
            standing=standing,
            standing_out_of_position=out_of_position,
            wheelchair=wheelchair,
            people_on_board=occupants + reading.children_in_arms,
            percentage=percentage,
            state=state,
            free_seats=free_seats,
            free_standing=seat_map.free_count(PositionKind.STANDING),
            wheelchair_free=seat_map.free_count(PositionKind.WHEELCHAIR) > 0,
            confidence=confidence,
            approximate_count=approximate,
        )

    @staticmethod
    def _from_door_count(capacity: BusCapacity, reading: Reading) -> tuple[int, int, int, int]:
        """Distribute the door-sensor total: seats first, then wheelchair, then standing."""
        total = reading.doors_total
        seated = min(reading.seated, capacity.seats, total)
        wheelchair = min(reading.wheelchair_occupied, capacity.wheelchair_spaces, total - seated)
        remaining = max(0, total - seated - wheelchair)
        standing = min(remaining, capacity.standing_positions)
        return seated, standing, wheelchair, remaining - standing

    @staticmethod
    def _classify(free_seats: int, occupants: int, total_capacity: int, out_of_position: int) -> OccupancyState:
        if out_of_position > 0 or occupants > total_capacity:
            return OccupancyState.OVERCROWDED
        if occupants == total_capacity:
            return OccupancyState.FULL
        if free_seats > 0:
            return OccupancyState.AVAILABLE_SEATS
        return OccupancyState.STANDING_ONLY


#: Confidence ceiling when the total comes from the door sensors instead of the cameras.
DOOR_COUNT_CONFIDENCE = 0.5
