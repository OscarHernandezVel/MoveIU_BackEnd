"""Sliding-window rate limiters.

Two implementations share the :class:`RateLimiter` contract:

* :class:`SlidingWindowRateLimiter` - in-process. Per client key it keeps a :class:`CircularArray`
  holding the timestamps of the last ``limit`` accepted requests. The request is allowed when that
  ring is not full or its *oldest* timestamp is older than the window, so memory per client is
  fixed and every check is O(1). Keys are kept in a doubly linked list used as an LRU (a hit moves
  the node to the front in O(1); the tail is evicted when ``max_keys`` is reached).
* :class:`RedisSlidingWindowRateLimiter` - the same policy shared by every replica (a sorted set
  per key, evaluated atomically by a Lua script). A client cannot multiply its quota by hitting
  different replicas.
"""

from __future__ import annotations

import secrets
import threading
from dataclasses import dataclass
from typing import Protocol

import redis
from redis.exceptions import RedisError

from core_service.clock import Clock
from core_service.data_structures import CircularArray, DoublyLinkedList, Node
from core_service.errors import ServiceUnavailableError


@dataclass(frozen=True)
class RateDecision:
    allowed: bool
    retry_after_seconds: float = 0.0
    remaining: int = 0


class RateLimiter(Protocol):
    def check(self, key: str) -> RateDecision: ...


@dataclass
class _Entry:
    key: str
    timestamps: CircularArray[float]


class SlidingWindowRateLimiter:
    def __init__(self, limit: int, window_seconds: float, clock: Clock, max_keys: int = 10_000) -> None:
        if limit < 1 or window_seconds <= 0 or max_keys < 1:
            raise ValueError("limit, window and max_keys must be positive")
        self._limit = limit
        self._window = window_seconds
        self._clock = clock
        self._max_keys = max_keys
        self._lru: DoublyLinkedList[_Entry] = DoublyLinkedList()
        self._nodes: dict[str, Node[_Entry]] = {}
        self._lock = threading.Lock()

    def __len__(self) -> int:
        return len(self._nodes)

    def check(self, key: str) -> RateDecision:
        now = self._clock.now()
        with self._lock:
            entry = self._touch(key)
            ring = entry.timestamps
            if ring.is_full() and now - ring.oldest() < self._window:
                return RateDecision(
                    False, retry_after_seconds=round(self._window - (now - ring.oldest()), 3), remaining=0
                )
            ring.push(now)
            used = sum(1 for stamp in ring if now - stamp < self._window)
            return RateDecision(True, remaining=self._limit - used)

    def _touch(self, key: str) -> _Entry:
        """Return the entry of ``key`` and make it the most recently used one."""
        node = self._nodes.get(key)
        if node is not None:
            self._lru.move_to_front(node)  # O(1), no allocation, the handle in ``_nodes`` stays valid
            return node.value
        if len(self._nodes) >= self._max_keys:
            evicted = self._lru.pop_back()
            del self._nodes[evicted.key]
        entry = _Entry(key, CircularArray(self._limit))
        self._nodes[key] = self._lru.prepend(entry)
        return entry


#: KEYS[1] = window log (sorted set of request times). ARGV = now, window, limit, member.
_WINDOW_SCRIPT = """
local now = tonumber(ARGV[1])
local window = tonumber(ARGV[2])
local limit = tonumber(ARGV[3])
redis.call('ZREMRANGEBYSCORE', KEYS[1], '-inf', now - window)
local used = redis.call('ZCARD', KEYS[1])
if used >= limit then
    local oldest = redis.call('ZRANGE', KEYS[1], 0, 0, 'WITHSCORES')
    return {0, tostring(window - (now - tonumber(oldest[2]))), 0}
end
redis.call('ZADD', KEYS[1], now, ARGV[4])
redis.call('PEXPIRE', KEYS[1], math.ceil(window * 1000))
return {1, '0', limit - used - 1}
"""


class RedisSlidingWindowRateLimiter:
    def __init__(
        self, client: redis.Redis, limit: int, window_seconds: float, clock: Clock, prefix: str = "core"
    ) -> None:
        if limit < 1 or window_seconds <= 0:
            raise ValueError("limit and window must be positive")
        self._limit = limit
        self._window = window_seconds
        self._clock = clock
        self._prefix = prefix
        self._script = client.register_script(_WINDOW_SCRIPT)

    def check(self, key: str) -> RateDecision:
        now = self._clock.now()
        member = f"{now:.6f}:{secrets.token_hex(4)}"
        try:
            allowed, retry_after, remaining = self._script(
                keys=[f"{self._prefix}:rl:{key}"], args=[now, self._window, self._limit, member]
            )
        except RedisError as error:
            raise ServiceUnavailableError() from error
        if int(allowed):
            return RateDecision(True, remaining=int(remaining))
        return RateDecision(False, retry_after_seconds=round(float(retry_after), 3), remaining=0)


class ResilientRateLimiter:
    """Shared limiter that keeps working when Redis is down.

    A rate limiter protects the service; it must never be the reason the service is unavailable.
    While the shared limiter fails, each replica falls back to its own in-process limiter (the quota
    is then per replica instead of global, which is a safe degradation) and recovers by itself."""

    def __init__(self, primary: RateLimiter, fallback: RateLimiter) -> None:
        self._primary = primary
        self._fallback = fallback

    def check(self, key: str) -> RateDecision:
        try:
            return self._primary.check(key)
        except ServiceUnavailableError:
            return self._fallback.check(key)
