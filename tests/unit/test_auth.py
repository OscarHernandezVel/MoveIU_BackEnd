"""Authentication: passwords, tokens, brute-force protection and sessions."""

from __future__ import annotations

import base64
import json

import jwt
import pytest

from core_service.auth import (
    AuthService,
    LoginAttemptTracker,
    PasswordHasher,
    PasswordPolicy,
    TokenService,
)
from core_service.clock import FakeClock
from core_service.config import Settings
from core_service.errors import (
    AuthenticationError,
    ConflictError,
    NotFoundError,
    RateLimitedError,
    ValidationError,
)
from core_service.metrics import MetricsRegistry
from core_service.models import Role
from core_service.persistence.documents import InMemoryDocumentStore
from core_service.persistence.relational import DeviceRepository, Migrator, UserRepository
from pg import make_database

NOW = 1_790_000_000.0
SETTINGS = Settings.for_testing().security
GOOD_PASSWORD = "Buses2026"
DEVICE = "device-abc-123456"


class TestPasswordPolicy:
    policy = PasswordPolicy()

    def test_accepts_a_valid_password(self) -> None:
        assert self.policy.violations(GOOD_PASSWORD) == []
        self.policy.validate(GOOD_PASSWORD)

    @pytest.mark.parametrize(
        ("password", "expected"),
        [
            ("Ab1", ["password_too_short"]),
            ("buses2026", ["password_no_uppercase"]),
            ("BusesPasto", ["password_no_digit"]),
            ("abc", ["password_too_short", "password_no_digit", "password_no_uppercase"]),
            ("A1" + "x" * 200, ["password_too_long"]),
        ],
    )
    def test_reports_every_broken_rule(self, password: str, expected: list[str]) -> None:
        assert self.policy.violations(password) == expected

    def test_validate_carries_params_for_localisation(self) -> None:
        with pytest.raises(ValidationError) as caught:
            self.policy.validate("abc")
        assert caught.value.params == {"min_length": 8, "max_length": 128}
        assert "password_no_digit" in caught.value.details


class TestPasswordHasher:
    hasher = PasswordHasher(iterations=1_000)

    def test_round_trip_and_unique_salts(self) -> None:
        first, second = self.hasher.hash(GOOD_PASSWORD), self.hasher.hash(GOOD_PASSWORD)
        assert first != second
        assert first.startswith("pbkdf2_sha256$1000$")
        assert self.hasher.verify(GOOD_PASSWORD, first) and self.hasher.verify(GOOD_PASSWORD, second)
        assert not self.hasher.verify("wrong", first)
        assert GOOD_PASSWORD not in first

    @pytest.mark.parametrize("garbage", ["", "plain", "md5$1$a$b", "pbkdf2_sha256$x$y$z", "pbkdf2_sha256$1000$!!$!!"])
    def test_malformed_hashes_never_verify(self, garbage: str) -> None:
        assert self.hasher.verify(GOOD_PASSWORD, garbage) is False

    def test_needs_rehash_when_the_cost_is_raised(self) -> None:
        stored = PasswordHasher(iterations=1_000).hash(GOOD_PASSWORD)
        assert PasswordHasher(iterations=2_000).needs_rehash(stored) is True
        assert PasswordHasher(iterations=1_000).needs_rehash(stored) is False
        assert PasswordHasher(iterations=1_000).needs_rehash("junk") is True

    def test_minimum_cost(self) -> None:
        with pytest.raises(ValueError):
            PasswordHasher(iterations=10)


class TestTokenService:
    def make(self) -> tuple[TokenService, FakeClock]:
        clock = FakeClock(NOW)
        return TokenService(SETTINGS, clock), clock

    def test_access_token_round_trip(self) -> None:
        tokens, _ = self.make()
        pair, _ = tokens.issue_pair("user-1", Role.PASSENGER, DEVICE)
        principal = tokens.verify_access(pair.access_token)
        assert (principal.subject, principal.role, principal.device_id) == ("user-1", Role.PASSENGER, DEVICE)
        assert pair.access_expires_at == NOW + 15 * 60

    def test_access_token_expires_with_the_clock(self) -> None:
        tokens, clock = self.make()
        pair, _ = tokens.issue_pair("user-1", Role.PASSENGER, None)
        clock.advance(15 * 60 - 1)
        tokens.verify_access(pair.access_token)
        clock.advance(2)
        with pytest.raises(AuthenticationError):
            tokens.verify_access(pair.access_token)

    def test_kinds_are_not_interchangeable(self) -> None:
        tokens, _ = self.make()
        pair, _ = tokens.issue_pair("user-1", Role.PASSENGER, None)
        with pytest.raises(AuthenticationError):
            tokens.verify_access(pair.refresh_token)
        with pytest.raises(AuthenticationError):
            tokens.verify_refresh(pair.access_token)

    def test_refresh_token_lasts_thirty_days(self) -> None:
        tokens, clock = self.make()
        pair, claims = tokens.issue_pair("user-1", Role.GUEST, DEVICE)
        assert claims.expires_at == NOW + 30 * 24 * 3600
        clock.advance(29 * 24 * 3600)
        assert tokens.verify_refresh(pair.refresh_token).jti == claims.jti

    def test_tampered_and_foreign_tokens_are_rejected(self) -> None:
        tokens, clock = self.make()
        pair, _ = tokens.issue_pair("user-1", Role.PASSENGER, None)
        header, payload, signature = pair.access_token.split(".")
        forged = json.loads(base64.urlsafe_b64decode(payload + "=="))
        forged["role"] = "admin"
        forged_payload = base64.urlsafe_b64encode(json.dumps(forged).encode()).decode().rstrip("=")
        with pytest.raises(AuthenticationError):
            tokens.verify_access(f"{header}.{forged_payload}.{signature}")
        foreign = jwt.encode(
            {
                "iss": "capacidad-bus",
                "sub": "x",
                "iat": 1,
                "exp": NOW + 999,
                "jti": "j",
                "role": "admin",
                "use": "access",
            },
            "another-secret-another-secret-123456",
            algorithm="HS256",
        )
        with pytest.raises(AuthenticationError):
            tokens.verify_access(foreign)
        with pytest.raises(AuthenticationError):
            tokens.verify_access("not-a-token")
        assert clock.now() == NOW

    def test_alg_none_is_rejected(self) -> None:
        tokens, _ = self.make()
        unsigned = jwt.encode(
            {
                "iss": "capacidad-bus",
                "sub": "x",
                "iat": 1,
                "exp": NOW + 999,
                "jti": "j",
                "role": "admin",
                "use": "access",
            },
            key=None,
            algorithm="none",
        )
        with pytest.raises(AuthenticationError):
            tokens.verify_access(unsigned)

    def test_wrong_issuer_and_unknown_role_are_rejected(self) -> None:
        tokens, _ = self.make()
        secret = SETTINGS.jwt_secret
        base = {"sub": "x", "iat": 1, "exp": NOW + 999, "jti": "j", "use": "access"}
        with pytest.raises(AuthenticationError):
            tokens.verify_access(jwt.encode({**base, "iss": "evil", "role": "admin"}, secret, algorithm="HS256"))
        with pytest.raises(AuthenticationError):
            tokens.verify_access(
                jwt.encode({**base, "iss": "capacidad-bus", "role": "superuser"}, secret, algorithm="HS256")
            )

    def test_unsupported_algorithms_are_refused_at_construction(self) -> None:
        from dataclasses import replace

        with pytest.raises(ValueError):
            TokenService(replace(SETTINGS, jwt_algorithm="none"), FakeClock(NOW))


class TestLoginAttemptTracker:
    def test_three_consecutive_failures_lock_for_five_minutes(self) -> None:
        clock = FakeClock(NOW)
        tracker = LoginAttemptTracker(clock, max_attempts=3, lock_seconds=300)
        for _ in range(2):
            tracker.record_failure("ana@example.com")
            assert tracker.lock_remaining("ana@example.com") is None
        tracker.record_failure("ana@example.com")
        assert tracker.lock_remaining("ana@example.com") == 300
        clock.advance(120)
        assert tracker.lock_remaining("ANA@example.com") == 180  # case-insensitive key
        clock.advance(181)
        assert tracker.lock_remaining("ana@example.com") is None

    def test_success_drains_the_failures(self) -> None:
        tracker = LoginAttemptTracker(FakeClock(NOW))
        tracker.record_failure("ana@example.com")
        tracker.record_failure("ana@example.com")
        tracker.record_success("ana@example.com")
        tracker.record_failure("ana@example.com")
        assert tracker.lock_remaining("ana@example.com") is None

    def test_failures_spread_over_a_long_time_do_not_lock(self) -> None:
        clock = FakeClock(NOW)
        tracker = LoginAttemptTracker(clock, max_attempts=3, lock_seconds=300)
        for _ in range(3):
            tracker.record_failure("ana@example.com")
            clock.advance(400)
        assert tracker.lock_remaining("ana@example.com") is None

    def test_after_a_lock_expires_counting_starts_again(self) -> None:
        clock = FakeClock(NOW)
        tracker = LoginAttemptTracker(clock)
        for _ in range(3):
            tracker.record_failure("ana@example.com")
        clock.advance(301)
        tracker.record_failure("ana@example.com")
        assert tracker.lock_remaining("ana@example.com") is None

    def test_accounts_are_independent(self) -> None:
        tracker = LoginAttemptTracker(FakeClock(NOW))
        for _ in range(3):
            tracker.record_failure("ana@example.com")
        assert tracker.lock_remaining("luis@example.com") is None


@pytest.fixture
def auth() -> tuple[AuthService, FakeClock, InMemoryDocumentStore, UserRepository, DeviceRepository]:
    clock = FakeClock(NOW)
    db = make_database()
    Migrator(db, clock).migrate()
    users, devices = UserRepository(db), DeviceRepository(db)
    store = InMemoryDocumentStore(clock)
    hasher = PasswordHasher(iterations=1_000)
    service = AuthService(
        users,
        devices,
        hasher,
        PasswordPolicy(),
        TokenService(SETTINGS, clock),
        LoginAttemptTracker(clock, 3, 300),
        store,
        clock,
        MetricsRegistry(),
    )
    return service, clock, store, users, devices


class TestAuthService:
    def test_guest_access_needs_no_personal_data(self, auth) -> None:
        service, _, _, _, devices = auth
        pair = service.guest(DEVICE, "android", "push-token")
        assert pair.role is Role.GUEST
        assert devices.find(DEVICE) is not None

    @pytest.mark.parametrize(
        ("device_id", "platform", "token"),
        [
            ("short", "android", "t"),
            (DEVICE, "windows", "t"),
            (DEVICE, "ios", ""),
            ("bad id with spaces!!", "ios", "t"),
        ],
    )
    def test_guest_input_is_validated(self, auth, device_id: str, platform: str, token: str) -> None:
        with pytest.raises(ValidationError):
            auth[0].guest(device_id, platform, token)

    def test_register_links_the_guest_device_and_ignores_any_role(self, auth) -> None:
        service, _, _, users, devices = auth
        service.guest(DEVICE, "android", "push-token")
        pair = service.register("Ana", "Ana@Example.com", "+573001112233", GOOD_PASSWORD, DEVICE)
        assert pair.role is Role.PASSENGER
        user = users.find_by_email("ana@example.com")
        assert user is not None and user.role is Role.PASSENGER
        linked = devices.find(DEVICE)
        assert linked is not None and linked.user_id == user.user_id

    def test_register_rejects_weak_passwords_bad_data_and_duplicates(self, auth) -> None:
        service = auth[0]
        with pytest.raises(ValidationError):
            service.register("Ana", "ana@example.com", None, "weak")
        with pytest.raises(ValidationError):
            service.register("Ana", "not-an-email", None, GOOD_PASSWORD)
        with pytest.raises(ValidationError):
            service.register("", "ana@example.com", None, GOOD_PASSWORD)
        with pytest.raises(ValidationError):
            service.register("Ana", "ana@example.com", "abc", GOOD_PASSWORD)
        service.register("Ana", "ana@example.com", None, GOOD_PASSWORD)
        with pytest.raises(ConflictError):
            service.register("Ana", "ANA@example.com", None, GOOD_PASSWORD)

    def test_register_with_an_unknown_device_creates_nothing(self, auth) -> None:
        service, _, _, users, _ = auth
        with pytest.raises(NotFoundError):
            service.register("Ana", "ana@example.com", None, GOOD_PASSWORD, "unknown-device-1")
        assert users.find_by_email("ana@example.com") is None

    def test_login_success_and_failure(self, auth) -> None:
        service = auth[0]
        service.register("Ana", "ana@example.com", None, GOOD_PASSWORD)
        assert service.login("ANA@example.com", GOOD_PASSWORD).role is Role.PASSENGER
        with pytest.raises(AuthenticationError) as wrong_password:
            service.login("ana@example.com", "Wrong1234")
        with pytest.raises(AuthenticationError) as unknown_user:
            service.login("nobody@example.com", GOOD_PASSWORD)
        assert wrong_password.value.code == unknown_user.value.code == "invalid_credentials"  # no user enumeration

    def test_lockout_after_three_failures_and_release_after_five_minutes(self, auth) -> None:
        service, clock, *_ = auth
        service.register("Ana", "ana@example.com", None, GOOD_PASSWORD)
        for _ in range(3):
            with pytest.raises(AuthenticationError):
                service.login("ana@example.com", "Wrong1234")
        with pytest.raises(RateLimitedError) as locked:
            service.login("ana@example.com", GOOD_PASSWORD)  # even the right password is refused
        assert locked.value.code == "account_locked" and locked.value.params["minutes"] == 5
        clock.advance(301)
        assert service.login("ana@example.com", GOOD_PASSWORD).role is Role.PASSENGER

    def test_a_locked_unknown_email_behaves_the_same(self, auth) -> None:
        service = auth[0]
        for _ in range(3):
            with pytest.raises(AuthenticationError):
                service.login("ghost@example.com", "Whatever1")
        with pytest.raises(RateLimitedError):
            service.login("ghost@example.com", "Whatever1")

    def test_refresh_is_single_use_and_rotates_tokens(self, auth) -> None:
        service = auth[0]
        first = service.guest(DEVICE, "android", "push-token")
        second = service.refresh(first.refresh_token)
        assert second.refresh_token != first.refresh_token
        with pytest.raises(AuthenticationError):
            service.refresh(first.refresh_token)  # reuse is rejected
        service.refresh(second.refresh_token)

    def test_refresh_picks_up_role_changes(self, auth) -> None:
        service, _, _, users, _ = auth
        pair = service.register("Ana", "ana@example.com", None, GOOD_PASSWORD)
        user = users.find_by_email("ana@example.com")
        assert user is not None
        users.set_role(user.user_id, Role.AUDITOR)
        assert service.refresh(pair.refresh_token).role is Role.AUDITOR

    def test_logout_revokes_the_refresh_token(self, auth) -> None:
        service = auth[0]
        pair = service.guest(DEVICE, "android", "push-token")
        service.logout(pair.refresh_token)
        with pytest.raises(AuthenticationError):
            service.refresh(pair.refresh_token)
        service.logout("garbage")  # never an error

    def test_expired_refresh_tokens_are_rejected(self, auth) -> None:
        service, clock, *_ = auth
        pair = service.guest(DEVICE, "android", "push-token")
        clock.advance(30 * 24 * 3600 + 1)
        with pytest.raises(AuthenticationError):
            service.refresh(pair.refresh_token)
