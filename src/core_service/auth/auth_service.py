"""Accounts and sessions: guest access, optional registration, login, refresh and logout."""

from __future__ import annotations

import math
import re
import uuid

from core_service.auth.login_attempts import LoginAttemptTracker
from core_service.auth.password_hasher import PasswordHasher
from core_service.auth.password_policy import PasswordPolicy
from core_service.auth.token_service import RefreshClaims, TokenPair, TokenService
from core_service.clock import Clock
from core_service.errors import AuthenticationError, NotFoundError, RateLimitedError, ValidationError
from core_service.metrics import MetricsRegistry
from core_service.models import Device, Role, User
from core_service.persistence.documents import DocumentStore
from core_service.persistence.relational import DeviceRepository, UserRepository

REFRESH_COLLECTION = "refresh_tokens"
_EMAIL = re.compile(r"^[^@\s]{1,64}@[^@\s]{1,255}\.[^@\s]{2,}$")
_PHONE = re.compile(r"^\+?[0-9]{7,15}$")
_DEVICE_ID = re.compile(r"^[A-Za-z0-9-]{8,64}$")


class AuthService:
    def __init__(
        self,
        users: UserRepository,
        devices: DeviceRepository,
        hasher: PasswordHasher,
        policy: PasswordPolicy,
        tokens: TokenService,
        attempts: LoginAttemptTracker,
        store: DocumentStore,
        clock: Clock,
        metrics: MetricsRegistry,
    ) -> None:
        self._users = users
        self._devices = devices
        self._hasher = hasher
        self._policy = policy
        self._tokens = tokens
        self._attempts = attempts
        self._store = store
        self._clock = clock
        self._metrics = metrics
        # Compared against when the e-mail does not exist, so response time does not reveal it.
        self._decoy_hash = hasher.hash(uuid.uuid4().hex)

    # ------------------------------------------------------------------ guest
    def guest(self, device_id: str, platform: str, push_token: str) -> TokenPair:
        """Story 1.0 / PB-10: use the app without an account and without personal data."""
        self._validate_device(device_id, platform, push_token)
        self._devices.upsert(Device(device_id, push_token, platform), self._clock.now())
        return self._issue(device_id, Role.GUEST, device_id)

    # --------------------------------------------------------------- register
    def register(
        self, name: str, email: str, phone: str | None, password: str, device_id: str | None = None
    ) -> TokenPair:
        """Story 1.1: optional account. Every public registration is a *passenger*; the roles
        ``auditor`` and ``admin`` can only be granted directly in the database."""
        clean_name = name.strip()
        clean_email = email.strip().lower()
        if not clean_name or len(clean_name) > 100:
            raise ValidationError("validation_error", details=["name must have between 1 and 100 characters"])
        if not _EMAIL.match(clean_email) or len(clean_email) > 150:
            raise ValidationError("validation_error", details=["email is not valid"])
        if phone is not None and not _PHONE.match(phone):
            raise ValidationError("validation_error", details=["phone is not valid"])
        self._policy.validate(password)
        if device_id is not None and self._devices.find(device_id) is None:
            raise NotFoundError("device_not_registered")

        user = User(
            user_id=str(uuid.uuid4()),
            name=clean_name,
            email=clean_email,
            phone=phone,
            password_hash=self._hasher.hash(password),
            role=Role.PASSENGER,
            created_at=self._clock.now(),
        )
        self._users.create(user)  # raises ConflictError("email_taken")
        linked = self._link_device(device_id, user.user_id)
        self._metrics.increment("auth_registered")
        return self._issue(user.user_id, user.role, linked)

    # ------------------------------------------------------------------ login
    def login(self, email: str, password: str, device_id: str | None = None) -> TokenPair:
        """Story 1.2: three consecutive failures lock the account for five minutes."""
        clean_email = email.strip().lower()
        remaining = self._attempts.lock_remaining(clean_email)
        if remaining is not None:
            self._metrics.increment("auth_locked")
            raise RateLimitedError("account_locked", params={"minutes": max(1, math.ceil(remaining / 60))})

        user = self._users.find_by_email(clean_email)
        password_ok = self._hasher.verify(password, user.password_hash if user else self._decoy_hash)
        if user is None or not password_ok:
            self._attempts.record_failure(clean_email)
            self._metrics.increment("auth_failed")
            raise AuthenticationError("invalid_credentials")

        self._attempts.record_success(clean_email)
        linked = self._link_device(device_id, user.user_id)
        return self._issue(user.user_id, user.role, linked)

    # ---------------------------------------------------------------- refresh
    def refresh(self, refresh_token: str) -> TokenPair:
        claims = self._tokens.verify_refresh(refresh_token)
        if not self._store.delete(REFRESH_COLLECTION, claims.jti):
            # A valid signature but no allow-list entry: the token was already used (or revoked).
            self._metrics.increment("auth_refresh_reuse")
            raise AuthenticationError("session_expired")
        role = claims.role
        if role is not Role.GUEST:
            user = self._users.find_by_id(claims.subject)
            if user is None:
                raise AuthenticationError("session_expired")
            role = user.role  # picks up role changes without a new login
        return self._issue(claims.subject, role, claims.device_id)

    def logout(self, refresh_token: str) -> None:
        try:
            claims = self._tokens.verify_refresh(refresh_token)
        except AuthenticationError:
            return  # signing out with an invalid token is not an error for the user
        self._store.delete(REFRESH_COLLECTION, claims.jti)

    # -------------------------------------------------------------- internals
    def _issue(self, subject: str, role: Role, device_id: str | None) -> TokenPair:
        pair, refresh = self._tokens.issue_pair(subject, role, device_id)
        self._remember(refresh)
        return pair

    def _remember(self, refresh: RefreshClaims) -> None:
        ttl = max(1.0, refresh.expires_at - self._clock.now())
        self._store.put(REFRESH_COLLECTION, refresh.jti, {"subject": refresh.subject}, ttl_seconds=ttl)

    def _link_device(self, device_id: str | None, user_id: str) -> str | None:
        """Attach the guest device (with its alerts) to the account; returns the linked id."""
        if device_id is None:
            return None
        if self._devices.link_user(device_id, user_id, self._clock.now()):
            return device_id
        raise NotFoundError("device_not_registered")

    @staticmethod
    def _validate_device(device_id: str, platform: str, push_token: str) -> None:
        problems = []
        if not _DEVICE_ID.match(device_id):
            problems.append("device_id must have 8 to 64 letters, digits or dashes")
        if platform not in ("android", "ios"):
            problems.append("platform must be android or ios")
        if not push_token or len(push_token) > 255:
            problems.append("push_token must have between 1 and 255 characters")
        if problems:
            raise ValidationError("validation_error", details=problems)
