"""Messages of the real-time WebSocket (``WS /ws/v1/buses/stream``).

Client -> server (JSON objects, at most ``max_message_bytes``)::

    {"action": "auth", "token": "<access token>"}          first message when no Authorization header was sent;
                                                          also used to renew the token before it expires
    {"action": "subscribe", "routes": ["C1", "E5"]}        v7 also accepts the Spanish key "rutas"
    {"action": "subscribe", "routes": ["C1"], "channel": "insights"}   staff only: route statistics for the AI team
    {"action": "unsubscribe", "routes": ["C1"]}
    {"action": "resync", "routes": ["C1"]}                 a sequence number was skipped: send a snapshot
    {"action": "pause"} / {"action": "resume"}             app in the background (RNF-2.2)
    {"action": "ping"}

Server -> client (``v`` is the protocol version)::

    {"v":1,"type":"snapshot","channel":"fleet","route":"C1","seq":7,"ts":..,"buses":[{bus}, ...]}
    {"v":1,"type":"delta","channel":"fleet","route":"C1","seq":8,"ts":..,"upsert":[{bus}],"remove":["2102"]}
    {"v":1,"type":"insights","route":"C1","seq":3,"ts":..,"data":{...}}
    {"v":1,"type":"subscribed"|"unsubscribed","channel":..,"routes":[..]}
    {"v":1,"type":"error","code":"...","detail":".."}  /  {"v":1,"type":"pong"}  /  {"v":1,"type":"heartbeat"}

A delta applies to the snapshot with ``seq - 1``; a client that sees a gap sends ``resync``.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from enum import Enum
from typing import Any

PROTOCOL_VERSION = 1
ROUTE_ID = re.compile(r"^[A-Za-z0-9-]{1,10}$")


class Channel(str, Enum):
    FLEET = "fleet"  # positions and occupancy for passengers (every role)
    INSIGHTS = "insights"  # headway and streaming statistics (admin and auditor)


class Action(str, Enum):
    AUTH = "auth"
    SUBSCRIBE = "subscribe"
    UNSUBSCRIBE = "unsubscribe"
    RESYNC = "resync"
    PAUSE = "pause"
    RESUME = "resume"
    PING = "ping"


class ProtocolError(Exception):
    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(code)
        self.code = code
        self.detail = detail


@dataclass(frozen=True)
class Command:
    action: Action
    routes: tuple[str, ...] = ()
    channel: Channel = Channel.FLEET
    token: str = ""


def parse_command(text: str, max_bytes: int, max_routes: int) -> Command:
    """Validate one client message. Raises :class:`ProtocolError` with a stable code."""
    if len(text.encode("utf-8")) > max_bytes:
        raise ProtocolError("message_too_large", f"at most {max_bytes} bytes")
    try:
        data = json.loads(text)
    except ValueError as error:
        raise ProtocolError("invalid_json") from error
    if not isinstance(data, dict):
        raise ProtocolError("invalid_message", "a JSON object is expected")
    try:
        action = Action(data.get("action"))
    except ValueError as error:
        raise ProtocolError("unknown_action") from error
    if action is Action.AUTH:
        token = data.get("token")
        if not isinstance(token, str) or not token.strip() or len(token) > 4_096:
            raise ProtocolError("invalid_token")
        return Command(action, token=token.strip())
    if action in (Action.SUBSCRIBE, Action.UNSUBSCRIBE, Action.RESYNC):
        return Command(action, _routes(data, max_routes), _channel(data))
    return Command(action)


def _routes(data: dict[str, Any], max_routes: int) -> tuple[str, ...]:
    raw = data.get("routes", data.get("rutas"))
    if not isinstance(raw, list) or not raw:
        raise ProtocolError("invalid_routes", "a non-empty list of route ids is expected")
    unique = list(dict.fromkeys(raw))
    if len(unique) > max_routes:
        raise ProtocolError("too_many_routes", f"at most {max_routes} routes")
    if not all(isinstance(route, str) and ROUTE_ID.match(route) for route in unique):
        raise ProtocolError("invalid_routes", "route ids are 1-10 letters, digits or dashes")
    return tuple(unique)


def _channel(data: dict[str, Any]) -> Channel:
    try:
        return Channel(data.get("channel", Channel.FLEET.value))
    except ValueError as error:
        raise ProtocolError("unknown_channel") from error


def frame(kind: str, **fields: Any) -> str:
    """Minimised JSON text of a server frame (RNF-2.1: light payloads)."""
    return json.dumps({"v": PROTOCOL_VERSION, "type": kind, **fields}, separators=(",", ":"), ensure_ascii=False)
