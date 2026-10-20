"""The Vercel entry (``app.py``): schema applied by the deploy step, no migrations or threads at start-up."""

from __future__ import annotations

import importlib.util
import time
from pathlib import Path
from types import ModuleType

import pytest
from fastapi.testclient import TestClient

import pg
from core_service.__main__ import migrate

ROOT = Path(__file__).resolve().parents[2]
API = "/api/v1"
CRON = "c" * 32
INGEST = "i" * 32


@pytest.fixture
def entry(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> ModuleType:
    db = pg.database_settings()
    env = {
        "CORE_DATABASE_URL": db.url,
        "CORE_DATABASE_SCHEMA": db.schema,
        "CORE_JWT_SECRET": "j" * 40,
        "CORE_INGEST_KEY": INGEST,
        "CORE_CRON_SECRET": CRON,
        "CORE_SEED_DEMO": "true",
        "CORE_PBKDF2_ITERATIONS": "1000",
    }
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    monkeypatch.delenv("CORE_DB_MIGRATE_ON_START", raising=False)
    migrate()  # what the deploy step runs: python -m core_service migrate
    assert "Migraciones aplicadas" in capsys.readouterr().out
    spec = importlib.util.spec_from_file_location("vercel_entry", ROOT / "app.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    pg.on_release(module.app.state.context.close)
    return module


def test_the_entry_starts_without_migrating_and_serves_requests(entry: ModuleType) -> None:
    context = entry.app.state.context
    assert context.settings.database.migrate_on_start is False
    assert context.settings.database.pool_max == 3
    with TestClient(entry.app) as client:  # run_jobs=False: no background thread was started
        assert client.get(f"{API}/health/live").status_code == 200
        guest = client.post(
            f"{API}/auth/guest", json={"device_id": "device-aaaa-0001", "platform": "android", "push_token": "t"}
        )
        headers = {"Authorization": f"Bearer {guest.json()['access_token']}"}
        assert [r["route_id"] for r in client.get(f"{API}/routes", headers=headers).json()["routes"]] == ["C5"]


def test_a_reading_with_its_cabin_reaches_the_twin_through_the_entry(entry: ModuleType) -> None:
    with TestClient(entry.app) as client:
        point = entry.app.state.context.route_catalog.require_track("C5").path.point_at(0.4)
        body = {
            "ts": time.time(), "lat": point.lat, "lng": point.lng, "seated": 2, "standing": 1, "doors_total": 3,
            "confidence": 0.9, "model_version": "m1", "cabin": {"occupied": ["S02-L1", "S03-R2", "P04"]},
        }  # fmt: skip
        assert (
            client.post(f"{API}/internal/buses/1045/telemetry", json=body, headers={"X-Ingest-Key": INGEST}).status_code
            == 200
        )
        guest = client.post(
            f"{API}/auth/guest", json={"device_id": "device-aaaa-0002", "platform": "ios", "push_token": "t"}
        ).json()
        cabin = client.get(
            f"{API}/buses/1045/cabin", headers={"Authorization": f"Bearer {guest['access_token']}"}
        ).json()
        assert cabin["fresh"] and cabin["occupied"] == ["P04", "S02-L1", "S03-R2"]


def test_the_scheduler_endpoint_needs_its_secret(entry: ModuleType) -> None:
    with TestClient(entry.app) as client:
        assert client.get(f"{API}/internal/jobs/run").status_code == 401
        assert client.get(f"{API}/internal/jobs/run", headers={"Authorization": "Bearer nope"}).status_code == 401
        assert client.get(f"{API}/internal/jobs/run", headers={"X-Ingest-Key": INGEST}).status_code == 401
        ok = client.get(f"{API}/internal/jobs/run", headers={"Authorization": f"Bearer {CRON}"})
        assert ok.status_code == 200 and isinstance(ok.json()["executed"], list)
        assert client.post(f"{API}/internal/jobs/run", headers={"Authorization": f"Bearer {CRON}"}).status_code == 200


def test_without_a_cron_secret_the_endpoint_is_off(monkeypatch: pytest.MonkeyPatch, entry: ModuleType) -> None:
    from dataclasses import replace

    context = entry.app.state.context
    context.settings = replace(context.settings, security=replace(context.settings.security, cron_secret=""))
    with TestClient(entry.app, raise_server_exceptions=False) as client:
        assert client.get(f"{API}/internal/jobs/run", headers={"Authorization": "Bearer "}).status_code == 503
