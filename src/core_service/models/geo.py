"""Geographic value objects and helpers."""

from __future__ import annotations

import math
from dataclasses import dataclass

from core_service.errors import ValidationError

EARTH_RADIUS_M = 6_371_008.8


@dataclass(frozen=True)
class GeoPoint:
    """WGS-84 coordinate in decimal degrees."""

    lat: float
    lng: float

    def __post_init__(self) -> None:
        if not (-90.0 <= self.lat <= 90.0) or not (-180.0 <= self.lng <= 180.0):
            raise ValidationError("invalid_coordinates", params={"lat": self.lat, "lng": self.lng})


def haversine_m(a: GeoPoint, b: GeoPoint) -> float:
    """Great-circle distance in metres."""
    phi_a, phi_b = math.radians(a.lat), math.radians(b.lat)
    d_phi = phi_b - phi_a
    d_lambda = math.radians(b.lng - a.lng)
    h = math.sin(d_phi / 2) ** 2 + math.cos(phi_a) * math.cos(phi_b) * math.sin(d_lambda / 2) ** 2
    return 2 * EARTH_RADIUS_M * math.asin(min(1.0, math.sqrt(h)))
