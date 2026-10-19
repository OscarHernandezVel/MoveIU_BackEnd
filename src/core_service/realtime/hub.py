"""Connection hub of the real-time stream: subscriptions, the tick loop and back-pressure.

Design (one hub per process; every replica runs its own, and all of them read the same Redis):

* **One read per tick for every route followed on this replica**: ``LiveStateStore.list_by_routes`` is
  one ``SMEMBERS`` per route plus a single ``MGET``. Cost per tick: O(routes + buses), independent of
  the number of connected phones. There is no per-client polling of Redis.
* **Fan-out of pre-serialised frames**: each route produces at most one delta text per tick
  (:class:`RouteFeed`); the same string goes to every subscriber.
* **Back-pressure**: each connection has a bounded outbox. A slow phone (bad 3G) whose outbox fills up
  does not slow anyone else: its queued deltas are dropped and it is sent a fresh snapshot of its routes
  (a snapshot replaces any number of lost deltas, so nothing is ever inconsistent).
* **Ordering**: feeds advance and new subscribers receive their first snapshot under one asyncio lock,
  so a snapshot at ``seq = N`` is always followed by the delta ``N + 1``.
* The blocking store and statistics reads run in worker threads (``asyncio.to_thread``); the event loop
  never waits on the network.
* The tick loop runs only while there are connections.

Horizontal scaling: every replica reads the shared live state, so a phone may connect to any replica
behind the load balancer (RNF-4.2); sequence numbers are per connection's replica and a reconnection
starts with a snapshot.
"""

from __future__ import annotations

import asyncio
import contextlib
import itertools
import logging
import time
from collections.abc import Awaitable, Callable
from typing import Any

from core_service.ai.features import FleetFeatureService
from core_service.clock import Clock
from core_service.config import OccupancySettings, RealtimeSettings
from core_service.metrics import MetricsRegistry
from core_service.models import LiveBusState, Principal, Role
from core_service.realtime.feed import InsightsFeed, RouteFeed
from core_service.realtime.protocol import Channel, ProtocolError, frame
from core_service.routing import RouteCatalog
from core_service.services.live_state_store import LiveStateStore

log = logging.getLogger(__name__)

STAFF_ROLES = frozenset({Role.ADMIN, Role.AUDITOR})
CLOSE_TOKEN_EXPIRED = 4401

SendText = Callable[[str], Awaitable[None]]
Close = Callable[[int, str], Awaitable[None]]
Key = tuple[Channel, str]


class Connection:
    """One phone (or staff console). Only the hub and the endpoint touch it, always on the event loop."""

    _ids = itertools.count(1)

    def __init__(self, send_text: SendText, close: Close, principal: Principal, outbox_size: int) -> None:
        self.id = next(self._ids)
        self.principal = principal
        self.subscriptions: set[Key] = set()
        self.needs_snapshot: set[Key] = set()
        self.paused = False
        self.overflows = 0
        self._send_text = send_text
        self._close = close
        self._outbox: asyncio.Queue[str] = asyncio.Queue(maxsize=outbox_size)
        self._last_sent = time.monotonic()
        self._closed = False
        self._sender: asyncio.Task[None] | None = None

    # ------------------------------------------------------------------ sending
    def start(self) -> None:
        self._sender = asyncio.create_task(self._pump(), name=f"ws-sender-{self.id}")

    def offer(self, text: str) -> bool:
        """Queue a frame without waiting. On overflow the queue is emptied and every route is marked for a
        snapshot (the client's view is rebuilt instead of being slowed down or left inconsistent)."""
        if self._closed:
            return False
        try:
            self._outbox.put_nowait(text)
            return True
        except asyncio.QueueFull:
            while not self._outbox.empty():
                self._outbox.get_nowait()
            self.needs_snapshot |= self.subscriptions
            self.overflows += 1
            return False

    async def _pump(self) -> None:
        try:
            while True:
                text = await self._outbox.get()
                await self._send_text(text)
                self._last_sent = time.monotonic()
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - the socket died; the receive loop will notice and unregister
            self._closed = True

    def idle_for(self) -> float:
        return time.monotonic() - self._last_sent

    async def close(self, code: int, reason: str) -> None:
        if self._closed:
            return
        self._closed = True
        with contextlib.suppress(Exception):
            await self._close(code, reason)

    async def stop(self) -> None:
        self._closed = True
        if self._sender is not None:
            self._sender.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._sender

    @property
    def closed(self) -> bool:
        return self._closed


class RealtimeHub:
    def __init__(
        self,
        live_states: LiveStateStore,
        catalog: RouteCatalog,
        clock: Clock,
        settings: RealtimeSettings,
        occupancy: OccupancySettings,
        metrics: MetricsRegistry,
        features: FleetFeatureService | None = None,
    ) -> None:
        self._live_states = live_states
        self._catalog = catalog
        self._clock = clock
        self.settings = settings
        self._stale_after = occupancy.no_data_after_seconds
        self._metrics = metrics
        self._features = features
        self._connections: dict[int, Connection] = {}
        self._subscribers: dict[Key, set[Connection]] = {}
        self._feeds: dict[str, RouteFeed] = {}
        self._insights: dict[str, InsightsFeed] = {}
        self._loop: asyncio.AbstractEventLoop | None = None
        self._lock: asyncio.Lock | None = None
        self._task: asyncio.Task[None] | None = None

    # ------------------------------------------------------------- lifecycle
    def _bind_loop(self) -> asyncio.Lock:
        loop = asyncio.get_running_loop()
        if self._loop is not loop or self._lock is None:
            self._loop, self._lock, self._task = loop, asyncio.Lock(), None
        return self._lock

    def can_accept(self) -> bool:
        return len(self._connections) < self.settings.max_connections

    @property
    def connection_count(self) -> int:
        return len(self._connections)

    def register(self, connection: Connection) -> None:
        self._bind_loop()
        self._connections[connection.id] = connection
        connection.start()
        self._metrics.increment("ws_connections_opened")
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._run(), name="ws-hub-tick")

    async def unregister(self, connection: Connection) -> None:
        self._connections.pop(connection.id, None)
        for key in list(connection.subscriptions):
            self._drop(connection, key)
        await connection.stop()
        self._metrics.increment("ws_connections_closed")

    async def shutdown(self) -> None:
        for connection in list(self._connections.values()):
            await connection.close(1001, "server shutting down")
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._task

    async def _run(self) -> None:
        while self._connections:
            started = time.perf_counter()
            try:
                await self.tick()
            except Exception:  # noqa: BLE001 - a failed tick (Redis blip) must not kill the stream
                self._metrics.increment("ws_tick_errors")
                log.exception("Real-time tick failed")
            self._metrics.observe("ws_tick_ms", (time.perf_counter() - started) * 1000.0)
            await asyncio.sleep(self.settings.tick_seconds)

    # ---------------------------------------------------------- subscriptions
    async def subscribe(self, connection: Connection, channel: Channel, routes: tuple[str, ...]) -> list[str]:
        if channel is Channel.INSIGHTS and connection.principal.role not in STAFF_ROLES:
            raise ProtocolError("forbidden_channel", "the insights channel is for admin and auditor roles")
        if channel is Channel.INSIGHTS and self._features is None:
            raise ProtocolError("channel_unavailable")
        unknown = [route for route in routes if self._catalog.find_track(route) is None]
        if unknown:
            raise ProtocolError("unknown_route", ",".join(unknown))
        wanted = {(channel, route) for route in routes}
        if len(connection.subscriptions | wanted) > self.settings.max_routes_per_connection:
            raise ProtocolError("too_many_routes", f"at most {self.settings.max_routes_per_connection} routes")
        lock = self._bind_loop()
        async with lock:
            for key in wanted:
                connection.subscriptions.add(key)
                self._subscribers.setdefault(key, set()).add(connection)
            connection.needs_snapshot |= wanted
            await self._prime(wanted)
            connection.offer(frame("subscribed", channel=channel.value, routes=list(routes)))
            self._flush_snapshots(connection)
        return list(routes)

    def unsubscribe(self, connection: Connection, channel: Channel, routes: tuple[str, ...]) -> None:
        for route in routes:
            self._drop(connection, (channel, route))

    async def resync(self, connection: Connection, channel: Channel, routes: tuple[str, ...]) -> None:
        lock = self._bind_loop()
        async with lock:
            connection.needs_snapshot |= {(channel, r) for r in routes} & connection.subscriptions
            self._flush_snapshots(connection)

    async def resume(self, connection: Connection) -> None:
        connection.paused = False
        await self.resync(
            connection, Channel.FLEET, tuple(r for c, r in connection.subscriptions if c is Channel.FLEET)
        )
        await self.resync(
            connection, Channel.INSIGHTS, tuple(r for c, r in connection.subscriptions if c is Channel.INSIGHTS)
        )

    def _drop(self, connection: Connection, key: Key) -> None:
        connection.subscriptions.discard(key)
        connection.needs_snapshot.discard(key)
        subscribers = self._subscribers.get(key)
        if subscribers is not None:
            subscribers.discard(connection)
            if not subscribers:
                del self._subscribers[key]
                channel, route = key
                if channel is Channel.FLEET:
                    self._feeds.pop(route, None)
                else:
                    self._insights.pop(route, None)

    async def _prime(self, keys: set[Key]) -> None:
        """Create the feeds a new subscription needs, so its first snapshot does not wait for a tick."""
        fleet = sorted(r for c, r in keys if c is Channel.FLEET and r not in self._feeds)
        insights = sorted(r for c, r in keys if c is Channel.INSIGHTS and r not in self._insights)
        if not fleet and not insights:
            return
        states = await asyncio.to_thread(self._live_states.list_by_routes, sorted(set(fleet) | set(insights)))
        now = self._clock.now()
        for route in fleet:
            feed = RouteFeed(route, self._stale_after)
            feed.advance(states.get(route, []), now)
            self._feeds[route] = feed
        for route in insights:
            feed_i = InsightsFeed(route)
            feed_i.advance(await self._route_insights(route, states.get(route, [])), now)
            self._insights[route] = feed_i

    def _flush_snapshots(self, connection: Connection) -> None:
        if connection.paused:
            return
        for key in sorted(connection.needs_snapshot):
            channel, route = key
            text = self._feeds[route].snapshot() if channel is Channel.FLEET and route in self._feeds else None
            if channel is Channel.INSIGHTS and route in self._insights:
                text = self._insights[route].current()
            if text is not None and connection.offer(text):
                connection.needs_snapshot.discard(key)

    # -------------------------------------------------------------------- tick
    async def tick(self) -> None:
        """Advance every followed route once and fan the frames out."""
        lock = self._bind_loop()
        async with lock:
            fleet = sorted(r for (c, r), subs in self._subscribers.items() if c is Channel.FLEET and subs)
            insights = sorted(r for (c, r), subs in self._subscribers.items() if c is Channel.INSIGHTS and subs)
            routes = sorted(set(fleet) | set(insights))
            states: dict[str, list[LiveBusState]] = {}
            if routes:
                states = await asyncio.to_thread(self._live_states.list_by_routes, routes)
            now = self._clock.now()
            for route in fleet:
                feed = self._feeds.setdefault(route, RouteFeed(route, self._stale_after))
                update = feed.advance(states.get(route, []), now)
                self._fan_out((Channel.FLEET, route), update.delta)
            for route in insights:
                feed_i = self._insights.setdefault(route, InsightsFeed(route))
                text = feed_i.advance(await self._route_insights(route, states.get(route, [])), now)
                self._fan_out((Channel.INSIGHTS, route), text)
        await self._housekeeping(now)

    def _fan_out(self, key: Key, text: str | None) -> None:
        for connection in list(self._subscribers.get(key, ())):
            if connection.paused:
                continue
            if key in connection.needs_snapshot:
                self._flush_snapshots(connection)
            elif text is not None and not connection.offer(text):
                self._metrics.increment("ws_backpressure_resets")
                self._flush_snapshots(connection)

    async def _route_insights(self, route: str, states: list[LiveBusState]) -> dict[str, Any]:
        assert self._features is not None
        return await asyncio.to_thread(self._features.route_features, route, states)

    async def _housekeeping(self, now: float) -> None:
        heartbeat = frame("heartbeat")
        for connection in list(self._connections.values()):
            expires = connection.principal.expires_at
            if expires is not None and now >= expires:
                await connection.close(CLOSE_TOKEN_EXPIRED, "token expired")
                continue
            if connection.idle_for() >= self.settings.heartbeat_seconds:
                connection.offer(heartbeat)
