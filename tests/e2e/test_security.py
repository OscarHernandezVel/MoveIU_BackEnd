"""Security behaviour visible from the outside: authentication, authorisation, abuse and injection."""

from __future__ import annotations

import base64
import json
from dataclasses import replace

import jwt
import pytest
from fastapi.testclient import TestClient

from core_service.api import create_app
from core_service.config import Settings
from pg import make_settings
from system_harness import API, PASSWORD, System, build_system

REGISTER = {"name": "Ana Pérez", "email": "ana@example.com", "password": PASSWORD}


class TestAuthentication:
    @pytest.mark.parametrize(
        "header", [None, "", "Bearer", "Bearer ", "Basic abc", "Bearer not.a.jwt", "bearer  x y z"]
    )
    def test_missing_or_malformed_credentials_are_rejected(self, system: System, header: str | None) -> None:
        headers = {} if header is None else {"Authorization": header}
        response = system.client.get(f"{API}/routes", headers=headers)
        assert response.status_code == 401 and response.headers["www-authenticate"] == "Bearer"
        assert response.json()["error"]["code"] in ("authentication_failed", "session_expired")

    def test_expired_tokens_are_rejected_and_refresh_renews_them(self, system: System) -> None:
        pair = system.client.post(
            f"{API}/auth/guest", json={"device_id": "device-aaaa-0001", "platform": "ios", "push_token": "t"}
        ).json()
        headers = {"Authorization": f"Bearer {pair['access_token']}"}
        assert system.client.get(f"{API}/routes", headers=headers).status_code == 200
        system.clock.advance(16 * 60)
        assert system.client.get(f"{API}/routes", headers=headers).status_code == 401
        renewed = system.client.post(f"{API}/auth/refresh", json={"refresh_token": pair["refresh_token"]})
        assert renewed.status_code == 200
        assert (
            system.client.get(
                f"{API}/routes", headers={"Authorization": f"Bearer {renewed.json()['access_token']}"}
            ).status_code
            == 200
        )

    def test_a_refresh_token_works_only_once(self, system: System) -> None:
        pair = system.client.post(
            f"{API}/auth/guest", json={"device_id": "device-aaaa-0001", "platform": "ios", "push_token": "t"}
        ).json()
        assert (
            system.client.post(f"{API}/auth/refresh", json={"refresh_token": pair["refresh_token"]}).status_code == 200
        )
        assert (
            system.client.post(f"{API}/auth/refresh", json={"refresh_token": pair["refresh_token"]}).status_code == 401
        )

    def test_logout_revokes_the_refresh_token(self, system: System) -> None:
        pair = system.client.post(
            f"{API}/auth/guest", json={"device_id": "device-aaaa-0001", "platform": "ios", "push_token": "t"}
        ).json()
        assert (
            system.client.post(f"{API}/auth/logout", json={"refresh_token": pair["refresh_token"]}).status_code == 204
        )
        assert (
            system.client.post(f"{API}/auth/refresh", json={"refresh_token": pair["refresh_token"]}).status_code == 401
        )
        assert system.client.post(f"{API}/auth/logout", json={"refresh_token": "garbage-garbage"}).status_code == 204

    def test_an_access_token_cannot_be_used_as_a_refresh_token_and_vice_versa(self, system: System) -> None:
        pair = system.client.post(
            f"{API}/auth/guest", json={"device_id": "device-aaaa-0001", "platform": "ios", "push_token": "t"}
        ).json()
        assert (
            system.client.post(f"{API}/auth/refresh", json={"refresh_token": pair["access_token"]}).status_code == 401
        )
        assert (
            system.client.get(f"{API}/routes", headers={"Authorization": f"Bearer {pair['refresh_token']}"}).status_code
            == 401
        )

    def test_forged_tokens_are_rejected(self, system: System) -> None:
        now = system.clock.now()
        claims = {
            "iss": "capacidad-bus",
            "sub": "x",
            "iat": now,
            "exp": now + 900,
            "jti": "abc",
            "role": "admin",
            "use": "access",
        }
        wrong_key = jwt.encode(claims, "another-secret-another-secret-another", algorithm="HS256")
        assert system.client.get(f"{API}/metrics", headers={"Authorization": f"Bearer {wrong_key}"}).status_code == 401
        header = base64.urlsafe_b64encode(b'{"alg":"none","typ":"JWT"}').rstrip(b"=").decode()
        payload = base64.urlsafe_b64encode(json.dumps(claims).encode()).rstrip(b"=").decode()
        assert (
            system.client.get(f"{API}/metrics", headers={"Authorization": f"Bearer {header}.{payload}."}).status_code
            == 401
        )


class TestAccounts:
    def test_public_registration_always_creates_a_passenger(self, system: System) -> None:
        response = system.client.post(f"{API}/auth/register", json=REGISTER)
        assert response.status_code == 201 and response.json()["role"] == "passenger"
        assert (
            system.client.post(
                f"{API}/auth/register", json={**REGISTER, "email": "b@example.com", "role": "admin"}
            ).status_code
            == 422
        )

    def test_duplicate_email_and_weak_passwords(self, system: System) -> None:
        system.client.post(f"{API}/auth/register", json=REGISTER)
        assert system.client.post(f"{API}/auth/register", json=REGISTER).status_code == 409
        weak = system.client.post(
            f"{API}/auth/register", json={**REGISTER, "email": "c@example.com", "password": "short"}
        )
        assert (
            weak.status_code == 422
            and "contraseña" in weak.json()["error"]["details"][0].lower() + weak.json()["error"]["message"].lower()
        )

    def test_login_does_not_reveal_whether_the_email_exists(self, system: System) -> None:
        system.client.post(f"{API}/auth/register", json=REGISTER)
        wrong_password = system.client.post(
            f"{API}/auth/login", json={"email": "ana@example.com", "password": "Wr0ngPassword"}
        )
        unknown = system.client.post(
            f"{API}/auth/login", json={"email": "nobody@example.com", "password": "Wr0ngPassword"}
        )
        assert wrong_password.status_code == unknown.status_code == 401
        assert wrong_password.json()["error"]["message"] == unknown.json()["error"]["message"]

    def test_three_failures_lock_the_account_for_five_minutes(self, system: System) -> None:
        system.client.post(f"{API}/auth/register", json=REGISTER)
        bad = {"email": "ana@example.com", "password": "Wr0ngPassword"}
        assert [system.client.post(f"{API}/auth/login", json=bad).status_code for _ in range(3)] == [401, 401, 401]
        locked = system.client.post(f"{API}/auth/login", json={"email": "ana@example.com", "password": PASSWORD})
        assert locked.status_code == 429 and "5" in locked.json()["error"]["message"]
        system.clock.advance(301)
        assert (
            system.client.post(f"{API}/auth/login", json={"email": "ana@example.com", "password": PASSWORD}).status_code
            == 200
        )

    def test_a_guest_can_keep_their_alerts_after_creating_an_account(self, system: System) -> None:
        headers = system.guest()
        system.client.post(
            f"{API}/alerts", json={"route_id": "C5", "direction": "outbound", "stop_id": "P-022"}, headers=headers
        )
        pair = system.client.post(f"{API}/auth/register", json={**REGISTER, "device_id": "device-aaaa-0001"}).json()
        account = {"Authorization": f"Bearer {pair['access_token']}"}
        assert len(system.client.get(f"{API}/alerts", headers=account).json()["alerts"]) == 1

    def test_passwords_never_appear_in_responses_or_logs(
        self, system: System, caplog: pytest.LogCaptureFixture
    ) -> None:
        with caplog.at_level("DEBUG"):
            response = system.client.post(f"{API}/auth/register", json=REGISTER)
        assert PASSWORD not in response.text and PASSWORD not in caplog.text


class TestAuthorisation:
    def test_roles_are_enforced_on_every_staff_endpoint(self, system: System) -> None:
        guest = system.guest()
        for method, path in (
            ("get", "/metrics"),
            ("get", "/findings"),
            ("get", "/ai/runs"),
            ("get", "/audits/metrics?start=2026-01-01&end=2026-01-02"),
        ):
            assert getattr(system.client, method)(f"{API}{path}", headers=guest).status_code == 403, path

    def test_an_account_without_a_phone_cannot_use_alerts(self, system: System) -> None:
        pair = system.client.post(f"{API}/auth/register", json=REGISTER).json()
        headers = {"Authorization": f"Bearer {pair['access_token']}"}
        response = system.client.get(f"{API}/alerts", headers=headers)
        assert response.status_code == 422 and response.json()["error"]["code"] == "device_not_registered"


class TestInputHardening:
    def test_sql_injection_attempts_are_inert(self, system: System) -> None:
        headers = system.guest()
        payloads = ["C5' OR '1'='1", "C5'; DROP TABLE users;--", "1 UNION SELECT password_hash FROM users"]
        for payload in payloads:
            assert system.client.get(f"{API}/routes/{payload}", headers=headers).status_code == 404
            assert (
                system.client.get(
                    f"{API}/stops/{payload}/next-buses", params={"route_id": "C5"}, headers=headers
                ).status_code
                == 404
            )
            assert (
                system.client.get(f"{API}/buses/active", params={"route_id": payload}, headers=headers).status_code
                == 404
            )
            assert (
                system.client.post(
                    f"{API}/alerts",
                    json={"route_id": payload, "direction": "outbound", "stop_id": "P-022"},
                    headers=headers,
                ).status_code
                == 422
            )
            assert (
                system.client.post(f"{API}/auth/login", json={"email": payload, "password": payload}).status_code == 401
            )
        assert system.context.database.query_one("SELECT COUNT(*) AS n FROM users")["n"] == 0
        assert system.context.database.query_one("SELECT COUNT(*) AS n FROM routes")["n"] == 1

    def test_validation_errors_do_not_echo_the_submitted_values(self, system: System) -> None:
        response = system.client.post(
            f"{API}/auth/register", json={**REGISTER, "phone": "x" * 40, "password": "SecretValue123" * 20}
        )
        assert response.status_code == 422
        assert "SecretValue123" not in response.text and "x" * 40 not in response.text

    def test_oversized_bodies_are_rejected(self, system: System) -> None:
        settings = make_settings()
        small = build_system(replace(settings, security=replace(settings.security, max_body_bytes=2_000)))
        response = small.client.post(
            f"{API}/auth/login", content=b"{" + b" " * 5_000 + b"}", headers={"Content-Type": "application/json"}
        )
        assert response.status_code == 413 and response.json()["error"]["code"] == "payload_too_large"
        chunked = small.client.post(
            f"{API}/auth/login", content=(b" " * 1_000 for _ in range(10)), headers={"Content-Type": "application/json"}
        )
        assert chunked.status_code == 413

    def test_malformed_json_and_wrong_types(self, system: System) -> None:
        assert (
            system.client.post(
                f"{API}/auth/login", content=b"{not json", headers={"Content-Type": "application/json"}
            ).status_code
            == 422
        )
        assert system.client.post(f"{API}/auth/login", json=["a"]).status_code == 422
        assert system.client.post(f"{API}/auth/login", json={"email": 5, "password": {"a": 1}}).status_code == 422

    def test_unknown_paths_and_methods_use_the_error_format(self, system: System) -> None:
        for response in (system.client.get(f"{API}/nothing"), system.client.delete(f"{API}/routes")):
            assert response.status_code in (404, 405) and set(response.json()["error"]) == {
                "code",
                "message",
                "details",
                "request_id",
            }


class TestAbuseProtection:
    def test_rate_limit_per_client(self) -> None:
        settings = make_settings()
        limited = build_system(replace(settings, security=replace(settings.security, rate_limit_per_minute=5)))
        statuses = [limited.client.get(f"{API}/routes").status_code for _ in range(7)]
        assert statuses[:5] == [401] * 5 and statuses[5:] == [429, 429]
        response = limited.client.get(f"{API}/routes")
        assert int(response.headers["retry-after"]) >= 1 and response.json()["error"]["code"] == "rate_limited"
        assert limited.client.get(f"{API}/health/live").status_code == 200  # health checks are never throttled
        limited.clock.advance(61)
        assert limited.client.get(f"{API}/routes").status_code == 401

    def test_forwarded_header_is_ignored_unless_a_proxy_is_trusted(self) -> None:
        settings = make_settings()
        limited = build_system(replace(settings, security=replace(settings.security, rate_limit_per_minute=2)))
        codes = [
            limited.client.get(f"{API}/routes", headers={"X-Forwarded-For": f"10.0.0.{i}"}).status_code
            for i in range(4)
        ]
        assert codes == [401, 401, 429, 429]  # spoofing the header does not reset the limit
        proxied = build_system(
            replace(settings, security=replace(settings.security, rate_limit_per_minute=2, trust_proxy=True))
        )
        codes = [
            proxied.client.get(f"{API}/routes", headers={"X-Forwarded-For": f"10.0.0.{i}"}).status_code
            for i in range(4)
        ]
        assert codes == [401] * 4

    def test_readings_with_the_ingest_key_are_limited_per_bus_not_per_gateway(self) -> None:
        settings = make_settings()
        limited = build_system(replace(settings, security=replace(settings.security, rate_limit_per_minute=2)))
        # the whole fleet reports through one gateway address: three buses, two readings each, all accepted
        for step in range(2):
            for bus_id in ("1045", "1102", "1088"):
                assert limited.ingest(bus_id, 0.2 + step * 0.01, ts_offset=step).status_code == 200
        assert limited.ingest("1045", 0.3, ts_offset=3).status_code == 429  # but each bus has its own budget
        wrong = {"X-Ingest-Key": "not-the-key"}
        codes = [
            limited.client.post(f"{API}/internal/buses/1102/telemetry", json=limited.reading(0.4), headers=wrong)
            for _ in range(3)
        ]
        assert [c.status_code for c in codes] == [401, 401, 429]  # a wrong key is limited per client address


class TestResponseHeaders:
    def test_security_headers_and_request_ids(self, system: System) -> None:
        response = system.client.get(f"{API}/health/live", headers={"X-Request-ID": "trace-1234-abcd"})
        assert response.headers["x-request-id"] == "trace-1234-abcd"
        assert response.headers["x-content-type-options"] == "nosniff" and response.headers["x-frame-options"] == "DENY"
        assert (
            response.headers["cache-control"] == "no-store"
            and "frame-ancestors 'none'" in response.headers["content-security-policy"]
        )
        assert "strict-transport-security" not in response.headers
        forged = system.client.get(f"{API}/health/live", headers={"X-Request-ID": "bad id\r\nX-Evil: 1"}).headers[
            "x-request-id"
        ]
        assert "\n" not in forged and forged != "bad id"

    def test_production_hides_the_docs_and_enables_hsts(self) -> None:
        # The HTTP layer only looks at ``settings.environment``. The data-layer rules of production
        # (TLS, Redis, Timescale) are covered in test_config.py, so here we flip just the flag.
        dev = build_system()
        base = dev.context.settings
        production = replace(
            base,
            environment="production",
            security=replace(base.security, pbkdf2_iterations=200_000, ingest_key="k" * 40),
        )
        prod_client = TestClient(create_app(replace(dev.context, settings=production)), raise_server_exceptions=False)
        assert prod_client.get("/docs").status_code == 404 and prod_client.get("/openapi.json").status_code == 404
        assert "max-age" in prod_client.get(f"{API}/health/live").headers["strict-transport-security"]
        assert dev.client.get("/docs").status_code == 200

    def test_cors_only_for_configured_origins(self) -> None:
        settings = make_settings()
        cors = build_system(
            replace(settings, security=replace(settings.security, cors_origins=("https://app.example.com",)))
        )
        allowed = cors.client.options(
            f"{API}/routes", headers={"Origin": "https://app.example.com", "Access-Control-Request-Method": "GET"}
        )
        denied = cors.client.options(
            f"{API}/routes", headers={"Origin": "https://evil.example.com", "Access-Control-Request-Method": "GET"}
        )
        assert allowed.headers.get("access-control-allow-origin") == "https://app.example.com"
        assert "access-control-allow-origin" not in denied.headers


class TestConfigurationGuards:
    def test_unsafe_production_configuration_is_refused(self) -> None:
        from core_service.errors import ValidationError

        with pytest.raises(ValidationError) as caught:
            Settings.from_env({"CORE_ENV": "production", "CORE_JWT_SECRET": "short"})
        assert len(caught.value.details) >= 3

    def test_secrets_are_required_and_generated_only_outside_production(self) -> None:
        dev = {"CORE_ENV": "development", "CORE_DATABASE_URL": "postgresql://core:core@localhost/core"}
        assert len(Settings.from_env(dev).security.jwt_secret) >= 32
        with pytest.raises(Exception):  # noqa: B017, PT011
            Settings.from_env({**dev, "CORE_AI_ENABLED": "true"})  # AI needs an API key
