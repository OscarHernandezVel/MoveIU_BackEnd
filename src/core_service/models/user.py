"""Users, devices and the authenticated principal."""

from __future__ import annotations

from dataclasses import dataclass

from core_service.models.enums import Role


@dataclass(frozen=True)
class User:
    user_id: str
    name: str
    email: str
    phone: str | None
    password_hash: str
    role: Role
    created_at: float


@dataclass(frozen=True)
class Device:
    """A phone, with or without an account (guest mode needs no personal data)."""

    device_id: str
    push_token: str
    platform: str
    user_id: str | None = None


@dataclass(frozen=True)
class Principal:
    """Identity extracted from a verified access token."""

    subject: str
    role: Role
    device_id: str | None
    #: Expiry of the token (epoch seconds); long-lived connections such as WebSockets close at this time.
    expires_at: float | None = None
