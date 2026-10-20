"""``WS /ws/v1/buses/stream``: live positions and occupancy of the buses of the routes a client follows.

Contract in :mod:`core_service.realtime.protocol` and ``docs/realtime.md``. Authentication uses the same
access tokens as the REST API (RNF-5.2): an ``Authorization: Bearer`` header on the upgrade request or,
for clients that cannot set headers, an ``auth`` message within ``auth_timeout_seconds``. Tokens are not
accepted in the URL (they would end up in proxy logs). Use ``wss://`` in production (RNF-5.1).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import time

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from core_service.container import ApplicationContext
from core_service.errors import DomainError
from core_service.models import Principal
from core_service.realtime.hub import Connection, RealtimeHub
from core_service.realtime.protocol import Action, Command, ProtocolError, frame, parse_command

router = APIRouter()
STREAM_PATH = "/ws/v1/buses/stream"
CLOSE_POLICY = 1008
CLOSE_OVERLOADED = 1013
CLOSE_UNAUTHENTICATED = 4401
MAX_PROTOCOL_ERRORS = 10


class _Budget:
    """Token bucket for client messages (a phone needs a handful per minute)."""

    def __init__(self, per_minute: int) -> None:
        self._capacity = float(max(1, per_minute))
        self._tokens = self._capacity
        self._rate = self._capacity / 60.0
        self._last = time.monotonic()

    def take(self) -> bool:
        now = time.monotonic()
        self._tokens = min(self._capacity, self._tokens + (now - self._last) * self._rate)
        self._last = now
        if self._tokens < 1.0:
            return False
        self._tokens -= 1.0
        return True


def _verify(context: ApplicationContext, token: str) -> Principal | None:
    try:
        return context.tokens.verify_access(token)
    except DomainError:
        return None


async def _authenticate(websocket: WebSocket, context: ApplicationContext) -> Principal | None:
    header = websocket.headers.get("authorization", "")
    scheme, _, token = header.partition(" ")
    if scheme.lower() == "bearer" and token.strip():
        return _verify(context, token.strip())
    settings = context.settings.realtime
    try:
        text = await asyncio.wait_for(websocket.receive_text(), timeout=settings.auth_timeout_seconds)
        command = parse_command(text, settings.max_message_bytes, settings.max_routes_per_connection)
    except (TimeoutError, ProtocolError, WebSocketDisconnect, RuntimeError):
        return None
    return _verify(context, command.token) if command.action is Action.AUTH else None


@router.websocket(STREAM_PATH)
async def bus_stream(websocket: WebSocket) -> None:
    context: ApplicationContext = websocket.app.state.context
    hub = context.realtime
    await websocket.accept()
    if hub is None or not hub.can_accept():
        await websocket.close(code=CLOSE_OVERLOADED, reason="try again later")
        return
    principal = await _authenticate(websocket, context)
    if principal is None:
        with contextlib.suppress(Exception):
            await websocket.close(code=CLOSE_UNAUTHENTICATED, reason="authentication required")
        return

    async def close(code: int, reason: str) -> None:
        await websocket.close(code=code, reason=reason)

    settings = context.settings.realtime
    connection = Connection(websocket.send_text, close, principal, settings.outbox_size)
    hub.register(connection)
    budget, errors = _Budget(settings.client_messages_per_minute), 0
    try:
        while not connection.closed:
            text = await websocket.receive_text()
            if not budget.take():
                await connection.close(CLOSE_POLICY, "too many messages")
                break
            try:
                command = parse_command(text, settings.max_message_bytes, settings.max_routes_per_connection)
                await _handle(hub, connection, context, command)
            except ProtocolError as error:
                errors += 1
                connection.offer(frame("error", code=error.code, detail=error.detail))
                if errors >= MAX_PROTOCOL_ERRORS:
                    await connection.close(CLOSE_POLICY, "too many invalid messages")
    except (WebSocketDisconnect, RuntimeError, json.JSONDecodeError):
        pass
    finally:
        await hub.unregister(connection)
        with contextlib.suppress(Exception):
            await websocket.close()


async def _handle(hub: RealtimeHub, connection: Connection, context: ApplicationContext, command: Command) -> None:
    if command.action is Action.SUBSCRIBE:
        await hub.subscribe(connection, command.channel, command.routes)  # acknowledges, then sends snapshots
    elif command.action is Action.UNSUBSCRIBE:
        hub.unsubscribe(connection, command.channel, command.routes)
        connection.offer(frame("unsubscribed", channel=command.channel.value, routes=list(command.routes)))
    elif command.action is Action.RESYNC:
        await hub.resync(connection, command.channel, command.routes)
    elif command.action is Action.PAUSE:
        connection.paused = True
    elif command.action is Action.RESUME:
        await hub.resume(connection)
    elif command.action is Action.PING:
        connection.offer(frame("pong"))
    elif command.action is Action.AUTH:  # renewal before the access token expires
        principal = _verify(context, command.token)
        if principal is None or principal.subject != connection.principal.subject:
            raise ProtocolError("invalid_token")
        connection.principal = principal
        connection.offer(frame("authenticated", expires_at=principal.expires_at))
