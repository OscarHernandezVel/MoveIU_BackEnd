"""Access and refresh tokens (JWT).

* Access token: 15 minutes, sent with every request and when opening the live stream.
* Refresh token: 30 days (a session lasts 30 days, story 1.2). It is single-use and tracked in
  an allow-list by :class:`AuthService`, so a stolen token stops working after one use.

Expiry is checked against the injected clock (not the machine clock) so it is testable, and
the accepted algorithm is pinned: ``alg=none`` and algorithm-confusion attacks are rejected.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from enum import Enum
from typing import Any

import jwt

from core_service.clock import Clock
from core_service.config import SecuritySettings
from core_service.errors import AuthenticationError
from core_service.models import Principal, Role

_ALLOWED_ALGORITHMS = {"HS256", "HS384", "HS512"}


class TokenKind(str, Enum):
    ACCESS = "access"
    REFRESH = "refresh"


@dataclass(frozen=True)
class TokenPair:
    access_token: str
    refresh_token: str
    access_expires_at: float
    role: Role


@dataclass(frozen=True)
class RefreshClaims:
    subject: str
    role: Role
    device_id: str | None
    jti: str
    expires_at: float


class TokenService:
    def __init__(self, settings: SecuritySettings, clock: Clock) -> None:
        if settings.jwt_algorithm not in _ALLOWED_ALGORITHMS:
            raise ValueError(f"unsupported JWT algorithm {settings.jwt_algorithm!r}")
        self._settings = settings
        self._clock = clock

    def _encode(
        self, kind: TokenKind, subject: str, role: Role, device_id: str | None, ttl: float
    ) -> tuple[str, str, float]:
        now = self._clock.now()
        jti = uuid.uuid4().hex
        expires_at = now + ttl
        claims: dict[str, Any] = {
            "iss": self._settings.jwt_issuer,
            "sub": subject,
            "iat": int(now),
            "exp": int(expires_at),
            "jti": jti,
            "role": role.value,
            "use": kind.value,
        }
        if device_id:
            claims["device"] = device_id
        token = jwt.encode(claims, self._settings.jwt_secret, algorithm=self._settings.jwt_algorithm)
        return token, jti, expires_at

    def issue_pair(self, subject: str, role: Role, device_id: str | None) -> tuple[TokenPair, RefreshClaims]:
        access, _, access_exp = self._encode(
            TokenKind.ACCESS, subject, role, device_id, self._settings.access_token_ttl
        )
        refresh, jti, refresh_exp = self._encode(
            TokenKind.REFRESH, subject, role, device_id, self._settings.refresh_token_ttl
        )
        pair = TokenPair(access, refresh, access_exp, role)
        return pair, RefreshClaims(subject, role, device_id, jti, refresh_exp)

    def _decode(self, token: str, kind: TokenKind) -> dict[str, Any]:
        try:
            claims: dict[str, Any] = jwt.decode(
                token,
                self._settings.jwt_secret,
                algorithms=[self._settings.jwt_algorithm],
                issuer=self._settings.jwt_issuer,
                options={"require": ["exp", "iat", "sub", "iss", "jti"], "verify_exp": False, "verify_iat": False},
            )
        except jwt.PyJWTError as error:
            raise AuthenticationError("session_expired") from error
        if claims.get("use") != kind.value:
            raise AuthenticationError("session_expired")
        if claims["exp"] <= self._clock.now():
            raise AuthenticationError("session_expired")
        try:
            Role(claims.get("role"))
        except ValueError as error:
            raise AuthenticationError("session_expired") from error
        return claims

    def verify_access(self, token: str) -> Principal:
        claims = self._decode(token, TokenKind.ACCESS)
        return Principal(
            subject=claims["sub"],
            role=Role(claims["role"]),
            device_id=claims.get("device"),
            expires_at=float(claims["exp"]) if "exp" in claims else None,
        )

    def verify_refresh(self, token: str) -> RefreshClaims:
        claims = self._decode(token, TokenKind.REFRESH)
        return RefreshClaims(
            subject=claims["sub"],
            role=Role(claims["role"]),
            device_id=claims.get("device"),
            jti=claims["jti"],
            expires_at=float(claims["exp"]),
        )
