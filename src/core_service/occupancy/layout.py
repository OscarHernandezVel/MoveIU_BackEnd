"""Provisional interior layout of a bus, built from its capacity.

The real map comes from the camera calibration (PB-09): one polygon per seat or standing position.
Until a bus is calibrated, the ingestion creates this provisional map so that the capacity check and
the seat map already have one row per position. Every generated position carries the
``UNCALIBRATED_ROI`` marker: the calibration tool replaces it, and nothing may treat it as a
real polygon.

Rules (from the design document): a standing position is the aisle between the two seats of a row,
so there is at most one per row with seats on both sides; the last row is the rear row of five
seats (only in vehicles big enough to have one); doors add zones but no capacity.
"""

from __future__ import annotations

from core_service.models import BusCapacity, BusPosition, Camera, PositionKind, PositionSide

REAR_ROW_SEATS = 5
MIN_SEATS_FOR_REAR_ROW = 10


def default_layout(capacity: BusCapacity) -> list[BusPosition]:
    rear_seats = REAR_ROW_SEATS if capacity.seats >= MIN_SEATS_FOR_REAR_ROW else 0
    body_seats = capacity.seats - rear_seats
    body_rows = max((body_seats + 1) // 2, 1)
    # A standing position needs a row with seats on both sides.
    full_rows = body_seats // 2
    if capacity.standing_positions > full_rows:
        raise ValueError(
            f"{capacity.standing_positions} standing positions do not fit between {full_rows} rows of seats"
        )
    last_row = body_rows + (1 if rear_seats else 0)
    middle = max(last_row // 2, 1)

    def camera(row: int) -> Camera:
        return Camera.FRONT if row <= middle else Camera.REAR

    positions: list[BusPosition] = []
    for row in range(1, body_rows + 1):
        for side in (PositionSide.LEFT, PositionSide.RIGHT):
            index = (row - 1) * 2 + (0 if side is PositionSide.LEFT else 1)
            if index < body_seats:
                positions.append(BusPosition(PositionKind.SEAT, row, side, camera(row)))
    for row in range(1, capacity.standing_positions + 1):
        positions.append(BusPosition(PositionKind.STANDING, row, PositionSide.AISLE, camera(row)))
    if rear_seats:
        positions.extend(
            BusPosition(PositionKind.SEAT, last_row, PositionSide.REAR, Camera.REAR) for _ in range(rear_seats)
        )
    for _ in range(capacity.wheelchair_spaces):
        positions.append(BusPosition(PositionKind.WHEELCHAIR, 1, PositionSide.LEFT, Camera.FRONT))
    door_rows = [1, max(middle, 1), last_row]
    for zone in range(capacity.door_zones):
        positions.append(
            BusPosition(PositionKind.DOOR, door_rows[zone % 3], PositionSide.RIGHT, camera(door_rows[zone % 3]))
        )
    return positions
