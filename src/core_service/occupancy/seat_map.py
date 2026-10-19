"""Seat map: an array with one cell per position of the bus interior.

Computing the occupancy is a single pass over the array (O(n)); asking whether a
given position is free is an index access (O(1)).
"""

from __future__ import annotations

from dataclasses import dataclass

from core_service.data_structures import Array
from core_service.models import BusCapacity
from core_service.models.layout import PositionKind

__all__ = ["Overflow", "Position", "PositionKind", "SeatMap"]


class Position:
    """One cell of the seat map."""

    __slots__ = ("kind", "occupied")

    def __init__(self, kind: PositionKind) -> None:
        self.kind = kind
        self.occupied = False


@dataclass(frozen=True)
class Overflow:
    """People that did not fit in the positions of their kind (they exceed capacity)."""

    seated: int = 0
    standing: int = 0
    wheelchair: int = 0

    @property
    def total(self) -> int:
        return self.seated + self.standing + self.wheelchair


class SeatMap:
    """Array of positions built from a :class:`BusCapacity`.

    Layout in the array: seats, standing positions, wheelchair spaces, door zones.
    """

    def __init__(self, capacity: BusCapacity) -> None:
        self._capacity = capacity
        layout = (
            [PositionKind.SEAT] * capacity.seats
            + [PositionKind.STANDING] * capacity.standing_positions
            + [PositionKind.WHEELCHAIR] * capacity.wheelchair_spaces
            + [PositionKind.DOOR] * capacity.door_zones
        )
        self._cells: Array[Position] = Array(max(len(layout), 1))
        for kind in layout:
            self._cells.append(Position(kind))

    def __len__(self) -> int:
        return len(self._cells)

    def position(self, index: int) -> Position:
        """O(1) access to a cell by index."""
        return self._cells[index]

    def place(self, seated: int, standing: int, wheelchair: int) -> Overflow:
        """Mark positions as occupied following the layout; returns the people left over."""
        remaining = {
            PositionKind.SEAT: seated,
            PositionKind.STANDING: standing,
            PositionKind.WHEELCHAIR: wheelchair,
        }
        for cell in self._cells:
            left = remaining.get(cell.kind, 0)
            if left > 0 and not cell.occupied:
                cell.occupied = True
                remaining[cell.kind] = left - 1
        return Overflow(
            seated=remaining[PositionKind.SEAT],
            standing=remaining[PositionKind.STANDING],
            wheelchair=remaining[PositionKind.WHEELCHAIR],
        )

    def occupied_count(self, kind: PositionKind) -> int:
        return self._cells.count_where(lambda cell: cell.kind is kind and cell.occupied)

    def free_count(self, kind: PositionKind) -> int:
        return self._cells.count_where(lambda cell: cell.kind is kind and not cell.occupied)
