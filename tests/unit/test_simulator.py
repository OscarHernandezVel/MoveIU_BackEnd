"""The demonstration fleet sends readings the real ingestion accepts."""

from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "simulator"))

import simulate_fleet as sim  # noqa: E402

from core_service.api.schemas import TelemetryBatchIn  # noqa: E402


def test_every_active_bus_of_the_23_routes_sends_valid_readings_with_its_cabin() -> None:
    fleet = sim.load_fleet(ROOT / "demo_data" / "pasto_todas.json", seed=7)
    assert len(fleet) == 45  # 46 buses, one in maintenance
    rush = sim.load_factor(datetime(2026, 11, 3, 18, 0, tzinfo=sim.PASTO))
    for _ in range(200):
        readings = [bus.step(3, rush) for bus in fleet]
    TelemetryBatchIn.model_validate({"readings": readings})
    for reading in readings:
        if "cabin" in reading:
            assert len(reading["cabin"]["occupied"]) == reading["seated"] + reading["standing"]


def test_rush_hours_carry_more_people_than_the_night() -> None:
    at = lambda hour: sim.load_factor(datetime(2026, 11, 3, hour, 0, tzinfo=sim.PASTO))  # noqa: E731
    assert at(18) > at(15) > at(23)
