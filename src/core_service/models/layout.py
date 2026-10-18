"""Interior map of a bus: every seat, standing position, wheelchair space and door zone."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from core_service.errors import ValidationError


class PositionKind(str, Enum):
    SEAT = "seat"
    STANDING = "standing"
    WHEELCHAIR = "wheelchair"
    DOOR = "door"


class PositionSide(str, Enum):
    LEFT = "left"
    RIGHT = "right"
    AISLE = "aisle"
    REAR = "rear"


class Camera(str, Enum):
    FRONT = "front"
    REAR = "rear"


#: Marker stored in ``roi`` until the camera calibration (PB-09) supplies the real polygon.
UNCALIBRATED_ROI: dict[str, Any] = {"calibrated": False, "polygon": []}


@dataclass(frozen=True)
class BusPosition:
    """One calibrated position. ``roi`` is the polygon of the position in the camera image."""

    kind: PositionKind
    row_no: int
    side: PositionSide
    camera: Camera
    roi: dict[str, Any] = field(default_factory=lambda: dict(UNCALIBRATED_ROI))

    def __post_init__(self) -> None:
        if self.row_no < 1:
            raise ValidationError("invalid_layout", details=["row_no must be at least 1"])
        polygon = self.roi.get("polygon")
        if self.roi.get("calibrated") and not (isinstance(polygon, list) and len(polygon) >= 3):
            raise ValidationError("invalid_layout", details=["a calibrated roi needs a polygon of 3 or more points"])

    @property
    def calibrated(self) -> bool:
        return bool(self.roi.get("calibrated"))
