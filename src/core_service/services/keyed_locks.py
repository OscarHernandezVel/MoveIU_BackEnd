"""Per-key mutual exclusion (e.g. one bus is processed by one worker at a time).

Two implementations share the :class:`LockManager` contract:

* :class:`KeyedLocks` - one re-entrant lock per key inside this process. Enough for one replica.
* :class:`RedisLockManager` - the same guarantee across *all* replicas, using ``SET NX PX`` with a
  random token and a compare-and-delete release script (a replica can never release a lock that
  expired and was taken by another one). The lock has a TTL, so a crashed replica cannot block a
  bus forever. Inside one process a local lock is taken first, so threads of the same replica
  queue locally instead of polling Redis, and nested acquisitions of the same key are re-entrant.
"""

from __future__ import annotations

import logging
import secrets
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import ContextManager, Protocol

import redis
from redis.exceptions import RedisError

from core_service.errors import ServiceUnavailableError

log = logging.getLogger(__name__)


class LockManager(Protocol):
    def acquire(self, key: str) -> ContextManager[None]: ...


class KeyedLocks:
    def __init__(self) -> None:
        self._locks: dict[str, threading.RLock] = {}
        self._guard = threading.Lock()

    @contextmanager
    def acquire(self, key: str) -> Iterator[None]:
        with self._guard:
            lock = self._locks.setdefault(key, threading.RLock())
        with lock:
            yield


_RELEASE_SCRIPT = """
if redis.call('GET', KEYS[1]) == ARGV[1] then
    return redis.call('DEL', KEYS[1])
end
return 0
"""


class RedisLockManager:
    def __init__(
        self,
        client: redis.Redis,
        prefix: str = "core",
        ttl_seconds: float = 30.0,
        wait_seconds: float = 10.0,
        poll_seconds: float = 0.02,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if ttl_seconds <= 0 or wait_seconds <= 0 or poll_seconds <= 0:
            raise ValueError("ttl, wait and poll must be positive")
        self._redis = client
        self._prefix = prefix
        self._ttl_ms = int(ttl_seconds * 1000)
        self._wait = wait_seconds
        self._poll = poll_seconds
        self._sleep = sleep
        self._release = client.register_script(_RELEASE_SCRIPT)
        self._local = KeyedLocks()
        self._depth: dict[str, int] = {}
        self._tokens: dict[str, str] = {}
        self._guard = threading.Lock()

    def _name(self, key: str) -> str:
        return f"{self._prefix}:lock:{key}"

    @contextmanager
    def acquire(self, key: str) -> Iterator[None]:
        with self._local.acquire(key):  # only one thread of this process talks to Redis for ``key``
            with self._guard:
                depth = self._depth.get(key, 0)
            if depth == 0:
                self._take(key)  # may raise: nothing to undo yet
            with self._guard:
                self._depth[key] = depth + 1
            try:
                yield
            finally:
                with self._guard:
                    remaining = self._depth[key] - 1
                    if remaining:
                        self._depth[key] = remaining
                    else:
                        del self._depth[key]
                if remaining == 0:
                    self._give_back(key)

    def _take(self, key: str) -> None:
        token = secrets.token_hex(16)
        deadline = time.monotonic() + self._wait
        try:
            while not self._redis.set(self._name(key), token, nx=True, px=self._ttl_ms):
                if time.monotonic() >= deadline:
                    log.error("Timed out waiting for lock %s", key)
                    raise ServiceUnavailableError()
                self._sleep(self._poll)
        except RedisError as error:
            raise ServiceUnavailableError() from error
        with self._guard:
            self._tokens[key] = token

    def _give_back(self, key: str) -> None:
        with self._guard:
            token = self._tokens.pop(key, None)
        if token is None:
            return
        try:
            released = self._release(keys=[self._name(key)], args=[token])
            if not released:
                log.warning("Lock %s expired before it was released (work took longer than the TTL)", key)
        except RedisError:
            log.warning("Could not release lock %s; it will expire by itself", key)
