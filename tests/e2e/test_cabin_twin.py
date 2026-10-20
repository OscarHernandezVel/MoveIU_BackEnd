"""The data behind the 3D twin: ids of occupied positions travel with the telemetry, never an image."""

from __future__ import annotations

from system_harness import API, INGEST, System

SEATED = ["S02-L1", "S02-R2", "S05-L1"]


class TestCabinTwin:
    def test_the_occupied_positions_of_the_last_reading_are_served_to_passengers(self, system: System) -> None:
        headers = system.guest()
        assert system.ingest("1045", 0.4, seated=3, standing=1, doors_total=4, cabin={"occupied": [*SEATED, "P04"]})
        body = system.client.get(f"{API}/buses/1045/cabin", headers=headers).json()
        assert body["fresh"] is True and body["occupied"] == sorted([*SEATED, "P04"])
        assert body["counts"]["seated"] == 3 and body["counts"]["people_on_board"] == 4

    def test_a_reading_without_cabin_keeps_working_and_shows_no_positions(self, system: System) -> None:
        headers = system.guest()
        assert system.ingest("1045", 0.4).status_code == 200
        body = system.client.get(f"{API}/buses/1045/cabin", headers=headers).json()
        assert body["occupied"] == [] and body["fresh"] is False

    def test_a_stale_picture_is_never_presented_as_current(self, system: System) -> None:
        headers = system.guest()
        system.ingest("1045", 0.4, seated=2, standing=0, doors_total=2, cabin={"occupied": SEATED[:2]})
        system.clock.advance(30)
        body = system.client.get(f"{API}/buses/1045/cabin", headers=headers).json()
        assert body["fresh"] is False and body["occupied"] == []

    def test_late_readings_do_not_overwrite_the_newest_picture(self, system: System) -> None:
        headers = system.guest()
        system.ingest("1045", 0.4, seated=2, standing=0, doors_total=2, cabin={"occupied": SEATED[:2]})
        late = system.ingest(
            "1045", 0.4, ts_offset=-60, seated=1, standing=0, doors_total=1, cabin={"occupied": ["S01-L1"]}
        )
        assert late.status_code == 200 and late.json()["status"] != "applied"
        assert system.client.get(f"{API}/buses/1045/cabin", headers=headers).json()["occupied"] == SEATED[:2]

    def test_batches_keep_the_newest_applied_picture(self, system: System) -> None:
        headers = system.guest()
        first = system.reading(0.4, ts_offset=-10, seated=1, doors_total=1) | {
            "bus_id": "1045",
            "cabin": {"occupied": ["S01-L1"]},
        }
        second = system.reading(0.41, seated=2, doors_total=2) | {
            "bus_id": "1045",
            "cabin": {"occupied": ["S01-L1", "S03-R1"]},
        }
        response = system.client.post(
            f"{API}/internal/telemetry/batch", json={"readings": [first, second]}, headers=INGEST
        )
        assert response.status_code == 200 and response.json()["accepted"] == 2
        assert system.client.get(f"{API}/buses/1045/cabin", headers=headers).json()["occupied"] == ["S01-L1", "S03-R1"]

    def test_invalid_position_ids_and_unknown_buses_are_rejected(self, system: System) -> None:
        headers = system.guest()
        bad = system.ingest("1045", 0.4, cabin={"occupied": ["not a position!"]})
        assert bad.status_code == 422
        too_many = system.ingest("1045", 0.4, cabin={"occupied": [f"S{i:02d}-L1" for i in range(81)]})
        assert too_many.status_code == 422
        assert system.client.get(f"{API}/buses/9999/cabin", headers=headers).status_code == 404
        assert system.client.get(f"{API}/buses/bad id!/cabin", headers=headers).status_code in (404, 422)
        assert system.client.get(f"{API}/buses/1045/cabin").status_code == 401
