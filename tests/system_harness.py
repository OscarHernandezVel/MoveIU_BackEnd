"""Whole-system harness: the whole system behind HTTP, with only the outside world replaced.

Replaced: the clock (deterministic time), the push provider, the team e-mail and the language
model. Everything else - routing, SQL, documents, events, auth, middleware - is the real code.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any

from fastapi.testclient import TestClient

from core_service.api import create_app
from core_service.auth import PasswordHasher
from core_service.clock import FakeClock
from core_service.config import AISettings, Settings
from core_service.container import ApplicationContext
from core_service.models import Role, User
from factories import demo_track
from fakes import RecordingNotifier, RecordingPushGateway, ScriptedLLMClient
from pg import make_settings, on_release

API = "/api/v1"
INGEST = {"X-Ingest-Key": "test-ingest-key-0123456789"}
TRACK = demo_track()
PASSWORD = "Str0ngPassw0rd"


@dataclass
class System:
    context: ApplicationContext
    client: TestClient
    clock: FakeClock
    push: RecordingPushGateway
    notifier: RecordingNotifier
    llm: ScriptedLLMClient

    # ------------------------------------------------------------------ sessions
    def guest(self, device_id: str = "device-aaaa-0001", token: str = "push-token-1") -> dict[str, str]:
        response = self.client.post(
            f"{API}/auth/guest", json={"device_id": device_id, "platform": "android", "push_token": token}
        )
        assert response.status_code == 200, response.text
        return {"Authorization": f"Bearer {response.json()['access_token']}"}

    def staff(self, role: Role, email: str) -> dict[str, str]:
        """Create a staff account directly in the database (public registration never grants roles) and sign in."""
        self.context.users.create(
            User(
                f"user-{email}",
                email.split("@")[0],
                email,
                None,
                PasswordHasher(1_000).hash(PASSWORD),
                role,
                self.clock.now(),
            )
        )
        response = self.client.post(f"{API}/auth/login", json={"email": email, "password": PASSWORD})
        assert response.status_code == 200, response.text
        return {"Authorization": f"Bearer {response.json()['access_token']}"}

    # ----------------------------------------------------------------- telemetry
    def reading(self, progress: float, *, ts_offset: float = 0.0, **fields: Any) -> dict[str, Any]:
        point = TRACK.path.point_at(progress)
        body = {
            "ts": self.clock.now() + ts_offset,
            "lat": point.lat,
            "lng": point.lng,
            "seated": 10,
            "standing": 0,
            "doors_total": 10,
            "confidence": 0.95,
            "model_version": "occ-1.2",
            "speed_kmh": 30.0,
        }
        body.update(fields)
        return body

    def ingest(self, bus_id: str, progress: float, *, ts_offset: float = 0.0, **fields: Any) -> Any:
        return self.client.post(
            f"{API}/internal/buses/{bus_id}/telemetry",
            json=self.reading(progress, ts_offset=ts_offset, **fields),
            headers=INGEST,
        )

    def drive(self, bus_id: str, start: float, stop: float, steps: int, **fields: Any) -> None:
        """Send ``steps`` readings, 5 s apart, moving evenly from ``start`` to ``stop`` (outbound)."""
        for step in range(steps):
            progress = start + (stop - start) * step / max(1, steps - 1)
            assert self.ingest(bus_id, progress, **fields).status_code == 200
            self.clock.advance(5)


def build_system(
    settings: Settings | None = None,
    *,
    ai_enabled: bool = True,
    llm_script: list | None = None,
    clock: FakeClock | None = None,
    **context_kwargs: Any,
) -> System:
    base = settings or make_settings()
    if ai_enabled:
        base = replace(base, ai=AISettings(enabled=True, api_key="test-key"))
    clock = clock or FakeClock(1_790_000_000.0)
    push, notifier, llm = RecordingPushGateway(), RecordingNotifier(), ScriptedLLMClient(llm_script or [])
    context = ApplicationContext.create(
        base,
        clock=clock,
        push_gateway=push,
        llm_client=llm if ai_enabled else None,
        team_notifier=notifier,
        sleep=lambda _seconds: None,
        seed_demo=True,
        **context_kwargs,
    )
    on_release(context.close)
    return System(context, TestClient(create_app(context), raise_server_exceptions=False), clock, push, notifier, llm)
