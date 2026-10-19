"""Request bodies. Every model rejects unknown fields and bounds every value (input validation)."""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

DeviceId = Field(min_length=8, max_length=64, pattern=r"^[A-Za-z0-9-]+$")


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class GuestIn(Strict):
    device_id: str = DeviceId
    platform: Literal["android", "ios"]
    push_token: str = Field(min_length=1, max_length=255)


class RegisterIn(Strict):
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(min_length=3, max_length=150)
    phone: str | None = Field(default=None, max_length=20)
    password: str = Field(min_length=1, max_length=128)
    device_id: str | None = Field(default=None, min_length=8, max_length=64, pattern=r"^[A-Za-z0-9-]+$")


class LoginIn(Strict):
    email: str = Field(min_length=3, max_length=150)
    password: str = Field(min_length=1, max_length=128)
    device_id: str | None = Field(default=None, min_length=8, max_length=64, pattern=r"^[A-Za-z0-9-]+$")


class RefreshIn(Strict):
    refresh_token: str = Field(min_length=10, max_length=2048)


class PushTokenIn(Strict):
    push_token: str = Field(min_length=1, max_length=255)


class AlertIn(Strict):
    route_id: str = Field(pattern=r"^[A-Za-z0-9-]{1,10}$")
    direction: Literal["outbound", "return"]
    stop_id: str = Field(pattern=r"^[A-Za-z0-9-]{1,20}$")
    stops_before: int = Field(default=2, ge=1, le=5)
    bus_id: str | None = Field(default=None, pattern=r"^[A-Za-z0-9-]{1,20}$")


class AuditIn(Strict):
    bus_id: str = Field(pattern=r"^[A-Za-z0-9-]{1,20}$")
    stop_id: str = Field(pattern=r"^[A-Za-z0-9-]{1,20}$")
    manual_seated: int = Field(ge=0, le=200)
    manual_standing: int = Field(ge=0, le=200)
    timestamp: float | None = Field(default=None, gt=0)


class CabinIn(Strict):
    """Ids of the occupied positions of the cabin (seats and standing positions): no image, no coordinates."""

    occupied: list[Annotated[str, StringConstraints(pattern=r"^[A-Z0-9-]{2,12}$")]] = Field(
        default_factory=list, max_length=80
    )


class TelemetryIn(Strict):
    ts: float = Field(gt=0)
    lat: float = Field(ge=-90, le=90)
    lng: float = Field(ge=-180, le=180)
    seated: int = Field(ge=0, le=500)
    standing: int = Field(ge=0, le=500)
    doors_total: int = Field(ge=0, le=1000)
    confidence: float = Field(ge=0, le=1)
    model_version: str = Field(min_length=1, max_length=40)
    standing_out_of_position: int = Field(default=0, ge=0, le=500)
    children_in_arms: int = Field(default=0, ge=0, le=100)
    wheelchair_occupied: int = Field(default=0, ge=0, le=20)
    approximate_count: bool = False
    speed_kmh: float | None = Field(default=None, ge=0, le=200)
    cabin: CabinIn | None = None


class TelemetryBatchItem(TelemetryIn):
    bus_id: str = Field(pattern=r"^[A-Za-z0-9-]{1,20}$")


class TelemetryBatchIn(Strict):
    readings: list[TelemetryBatchItem] = Field(min_length=1, max_length=500)


class CenterIn(Strict):
    lat: float = Field(ge=-90, le=90)
    lng: float = Field(ge=-180, le=180)


class AnalysisIn(Strict):
    query: str = Field(default="", max_length=300)
    route_ids: list[str] = Field(default_factory=list, max_length=5)
    center: CenterIn | None = None
    radius_km: float = Field(default=3.0, gt=0, le=20)
