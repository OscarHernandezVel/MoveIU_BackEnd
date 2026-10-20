"""``WS /ws/v1/buses/stream`` end to end: real app, real SQL, real hub; only time and the outside world faked."""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from dataclasses import replace
from typing import Any

import fakeredis
import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

import pg
from core_service.clock import FakeClock
from core_service.config import RealtimeSettings
from core_service.models import Principal, Role
from core_service.realtime import Channel, Connection
from system_harness import System, build_system

PATH = "/ws/v1/buses/stream"
FAST = RealtimeSettings(tick_seconds=0.03, heartbeat_seconds=0.15, auth_timeout_seconds=0.5)


def fast_system(**realtime: Any) -> System:
    return build_system(replace(pg.make_settings(), realtime=replace(FAST, **realtime)), ai_enabled=False)


@pytest.fixture
def system() -> System:
    return fast_system()


@pytest.fixture
def client(system: System) -> Iterator[TestClient]:
    with system.client as running:  # one event loop for every socket of the test
        yield running


def next_frame(ws: Any, kind: str, limit: int = 300) -> dict[str, Any]:
    """Next frame of ``kind``, skipping heartbeats (they also bound the wait: one every 0.15 s)."""
    for _ in range(limit):
        message = ws.receive_json()
        if message["type"] == kind:
            return message
        if message["type"] not in ("heartbeat", "subscribed"):
            raise AssertionError(f"expected {kind}, got {message}")
    raise AssertionError(f"no {kind} frame")


def subscribe(ws: Any, *routes: str, channel: str = "fleet") -> dict[str, Any]:
    ws.send_json({"action": "subscribe", "routes": list(routes), "channel": channel})
    ack = ws.receive_json()
    assert ack["type"] == "subscribed", ack
    return next_frame(ws, "insights" if channel == "insights" else "snapshot")


def closed_with(ws: Any) -> int:
    with pytest.raises(WebSocketDisconnect) as caught:
        for _ in range(100):
            ws.receive_json()
    return caught.value.code


class TestStream:
    def test_a_guest_follows_a_route_and_sees_a_bus_move(self, system: System, client: TestClient) -> None:
        with client.websocket_connect(PATH, headers=system.guest()) as ws:
            snapshot = subscribe(ws, "C5")
            assert snapshot["route"] == "C5" and snapshot["buses"] == []
            assert system.ingest("1045", 0.30, seated=38, standing=5, doors_total=43).status_code == 200
            delta = next_frame(ws, "delta")
            bus = delta["upsert"][0]
            assert delta["seq"] == snapshot["seq"] + 1 and bus["bus"] == "1045"
            assert (bus["seated"], bus["standing"], bus["pct"]) == (38, 5, 88)  # v7 example: (38+5)/49 = 88 %
            system.clock.advance(5)
            system.ingest("1045", 0.35)
            moved = next_frame(ws, "delta")
            assert moved["seq"] == delta["seq"] + 1 and moved["upsert"][0]["lat"] != bus["lat"]

    def test_a_joining_client_receives_the_current_state(self, system: System, client: TestClient) -> None:
        system.ingest("1045", 0.30)
        with client.websocket_connect(PATH, headers=system.guest()) as ws:
            snapshot = subscribe(ws, "C5")
            assert [b["bus"] for b in snapshot["buses"]] == ["1045"]

    def test_the_message_based_login_works_for_clients_without_headers(
        self, system: System, client: TestClient
    ) -> None:
        token = system.guest()["Authorization"].split()[1]
        with client.websocket_connect(PATH) as ws:
            ws.send_json({"action": "auth", "token": token})
            assert subscribe(ws, "C5")["type"] == "snapshot"

    def test_a_silent_bus_turns_into_no_data(self, system: System, client: TestClient) -> None:
        system.ingest("1045", 0.30)
        with client.websocket_connect(PATH, headers=system.guest()) as ws:
            subscribe(ws, "C5")
            system.clock.advance(61)
            assert next_frame(ws, "delta")["upsert"][0]["state"] == "no_data"

    def test_pause_stops_the_frames_and_resume_sends_a_snapshot(self, system: System, client: TestClient) -> None:
        with client.websocket_connect(PATH, headers=system.guest()) as ws:
            subscribe(ws, "C5")
            ws.send_json({"action": "pause"})
            system.ingest("1045", 0.30)
            for _ in range(4):  # several ticks pass: only heartbeats
                assert ws.receive_json()["type"] == "heartbeat"
            ws.send_json({"action": "resume"})
            assert [b["bus"] for b in next_frame(ws, "snapshot")["buses"]] == ["1045"]

    def test_resync_and_ping(self, system: System, client: TestClient) -> None:
        with client.websocket_connect(PATH, headers=system.guest()) as ws:
            first = subscribe(ws, "C5")
            ws.send_json({"action": "ping"})
            assert next_frame(ws, "pong")["type"] == "pong"
            ws.send_json({"action": "resync", "routes": ["C5"]})
            assert next_frame(ws, "snapshot")["seq"] == first["seq"]

    def test_unsubscribe_stops_the_route(self, system: System, client: TestClient) -> None:
        with client.websocket_connect(PATH, headers=system.guest()) as ws:
            subscribe(ws, "C5")
            ws.send_json({"action": "unsubscribe", "routes": ["C5"]})
            assert next_frame(ws, "unsubscribed")["routes"] == ["C5"]
            system.ingest("1045", 0.30)
            for _ in range(4):
                assert ws.receive_json()["type"] == "heartbeat"


class TestRules:
    def test_without_a_token_the_socket_is_closed(self, client: TestClient) -> None:
        with client.websocket_connect(PATH) as ws:
            ws.send_json({"action": "subscribe", "routes": ["C5"]})
            assert closed_with(ws) == 4401

    def test_an_invalid_header_token_is_rejected(self, client: TestClient) -> None:
        with client.websocket_connect(PATH, headers={"Authorization": "Bearer nope"}) as ws:
            assert closed_with(ws) == 4401

    def test_silence_after_connecting_is_rejected(self, client: TestClient) -> None:
        with client.websocket_connect(PATH) as ws:
            assert closed_with(ws) == 4401  # auth_timeout_seconds = 0.5

    def test_errors_are_reported_and_the_socket_stays_open(self, system: System, client: TestClient) -> None:
        with client.websocket_connect(PATH, headers=system.guest()) as ws:
            ws.send_json({"action": "subscribe", "routes": ["ZZ9"]})
            assert next_frame(ws, "error")["code"] == "unknown_route"
            ws.send_json({"action": "subscribe", "routes": ["C5", "C1", "C2", "C3", "C4", "E1"]})
            assert next_frame(ws, "error")["code"] == "too_many_routes"
            ws.send_text("not json")
            assert next_frame(ws, "error")["code"] == "invalid_json"
            assert subscribe(ws, "C5")["type"] == "snapshot"

    def test_the_insights_channel_is_for_staff(self, system: System, client: TestClient) -> None:
        with client.websocket_connect(PATH, headers=system.guest()) as ws:
            ws.send_json({"action": "subscribe", "routes": ["C5"], "channel": "insights"})
            assert next_frame(ws, "error")["code"] == "forbidden_channel"
        admin = system.staff(Role.ADMIN, "admin@example.com")
        system.ingest("1045", 0.30)
        with client.websocket_connect(PATH, headers=admin) as ws:
            data = subscribe(ws, "C5", channel="insights")["data"]
            assert set(data["headway"]) == {"outbound", "return"} and "1045" in data["buses"]

    def test_an_expired_token_closes_the_socket(self, system: System, client: TestClient) -> None:
        with client.websocket_connect(PATH, headers=system.guest()) as ws:
            subscribe(ws, "C5")
            system.clock.advance(16 * 60)  # access tokens last 15 minutes
            assert closed_with(ws) == 4401

    def test_capacity_is_enforced(self) -> None:
        system = fast_system(max_connections=1)
        with system.client as client, client.websocket_connect(PATH, headers=system.guest()) as first:
            subscribe(first, "C5")
            with client.websocket_connect(PATH, headers=system.guest()) as second:
                assert closed_with(second) == 1013


class TestReplicas:
    def test_a_phone_on_one_replica_sees_readings_handled_by_another(self) -> None:
        server = fakeredis.FakeServer()
        redis_client = fakeredis.FakeRedis(server=server, decode_responses=True)
        clock = FakeClock(1_790_000_000.0)
        settings = replace(pg.make_settings(), realtime=FAST)
        a = build_system(settings, ai_enabled=False, clock=clock, redis_client=redis_client)
        b = build_system(settings, ai_enabled=False, clock=clock, redis_client=redis_client)
        with b.client as client, client.websocket_connect(PATH, headers=b.guest()) as ws:
            subscribe(ws, "C5")
            assert a.ingest("1045", 0.30).status_code == 200  # replica A processes the reading
            assert next_frame(ws, "delta")["upsert"][0]["bus"] == "1045"


class TestBackPressure:
    def test_a_slow_client_is_switched_to_a_snapshot_instead_of_slowing_the_others(self) -> None:
        async def scenario() -> tuple[bool, set[Any], int]:
            blocked = asyncio.Event()

            async def send_text(_: str) -> None:
                await blocked.wait()  # the phone never reads

            async def close(_: int, __: str) -> None:
                return None

            connection = Connection(send_text, close, Principal("g", Role.GUEST, None), outbox_size=2)
            connection.subscriptions = {(Channel.FLEET, "C5")}
            connection.start()
            await asyncio.sleep(0)
            connection.offer("frame-0")
            await asyncio.sleep(0)  # the sender takes it and blocks on the socket
            results = [connection.offer(f"frame-{i}") for i in (1, 2, 3)]  # 1 and 2 fill the outbox
            await connection.stop()
            return results[-1], connection.needs_snapshot, connection.overflows

        accepted, needs_snapshot, overflows = asyncio.run(scenario())
        assert accepted is False and needs_snapshot == {(Channel.FLEET, "C5")} and overflows == 1
