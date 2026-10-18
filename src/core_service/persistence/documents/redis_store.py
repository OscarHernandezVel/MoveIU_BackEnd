"""Redis implementation of the document store, plus the shared-state primitives built on it.

Why Redis here: live bus state, de-duplication keys, refresh-token allow-list, screen inboxes and
counters are small, hot, short-lived and must be visible to *every* service replica at once.

Key layout (``<prefix>`` defaults to ``core``)::

    <prefix>:doc:<collection>:<key>   JSON string, native TTL          (put / get / set_if_absent)
    <prefix>:cnt:<collection>:<key>   integer, native TTL              (increment)
    <prefix>:set:<collection>:<key>   Redis SET                        (route index, alert index)

Every multi-step operation that must be atomic is a single command or a Lua script (executed
atomically by Redis). Expiry is done by Redis itself, so :meth:`purge_expired` has nothing to do.
Any Redis failure (server down, fail-over in progress, out of memory) is raised as
:class:`ServiceUnavailableError`, which the API turns into a 503.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Callable, Iterator
from typing import Any, TypeVar

import redis
from redis.exceptions import RedisError

from core_service.errors import ServiceUnavailableError, ValidationError
from core_service.persistence.documents.in_memory import serialize
from core_service.persistence.documents.store import Document, DocumentStore

log = logging.getLogger(__name__)
T = TypeVar("T")

_SCAN_COUNT = 500
_GLOB_SPECIALS = re.compile(r"([\\*?\[\]])")

#: INCRBY that sets the TTL only when the counter is created (fixed-window semantics).
_INCREMENT_SCRIPT = """
local created = redis.call('EXISTS', KEYS[1]) == 0
local value = redis.call('INCRBY', KEYS[1], ARGV[1])
if created and tonumber(ARGV[2]) > 0 then redis.call('PEXPIRE', KEYS[1], ARGV[2]) end
return value
"""


def _glob_escape(text: str) -> str:
    return _GLOB_SPECIALS.sub(r"\\\1", text)


def _millis(ttl_seconds: float | None) -> int | None:
    if ttl_seconds is None:
        return None
    if ttl_seconds <= 0:
        raise ValidationError("invalid_ttl")
    return max(1, int(ttl_seconds * 1000))


class RedisDocumentStore(DocumentStore):
    def __init__(self, client: redis.Redis, prefix: str = "core") -> None:
        self._redis = client
        self._prefix = prefix
        self._increment = client.register_script(_INCREMENT_SCRIPT)

    # ------------------------------------------------------------------ helpers
    def _doc(self, collection: str, key: str) -> str:
        return f"{self._prefix}:doc:{collection}:{key}"

    def _counter(self, collection: str, key: str) -> str:
        return f"{self._prefix}:cnt:{collection}:{key}"

    def _set(self, collection: str, key: str) -> str:
        return f"{self._prefix}:set:{collection}:{key}"

    @staticmethod
    def _call(action: Callable[[], T]) -> T:
        try:
            return action()
        except RedisError as error:
            log.error("Redis unavailable: %s", type(error).__name__)
            raise ServiceUnavailableError() from error

    def _scan(self, collection: str) -> Iterator[str]:
        pattern = f"{_glob_escape(self._prefix)}:doc:{_glob_escape(collection)}:*"
        yield from self._redis.scan_iter(match=pattern, count=_SCAN_COUNT)

    # --------------------------------------------------------------------- API
    def put(self, collection: str, key: str, document: Document, ttl_seconds: float | None = None) -> None:
        payload = serialize(document)
        self._call(lambda: self._redis.set(self._doc(collection, key), payload, px=_millis(ttl_seconds)))

    def get(self, collection: str, key: str) -> Document | None:
        raw: Any = self._call(lambda: self._redis.get(self._doc(collection, key)))
        if raw is None:
            return None
        value = json.loads(raw)
        return value if isinstance(value, dict) else None

    def get_many(self, collection: str, keys: list[str]) -> dict[str, Document]:
        """One ``MGET`` (one round-trip) instead of one ``GET`` per key."""
        if not keys:
            return {}
        names = [self._doc(collection, key) for key in keys]
        raws: Any = self._call(lambda: self._redis.mget(names))
        found: dict[str, Document] = {}
        for key, raw in zip(keys, raws, strict=True):
            if raw is None:
                continue
            value = json.loads(raw)
            if isinstance(value, dict):
                found[key] = value
        return found

    def delete(self, collection: str, key: str) -> bool:
        removed = self._call(lambda: self._redis.delete(self._doc(collection, key), self._counter(collection, key)))
        return bool(removed)

    def set_if_absent(self, collection: str, key: str, document: Document, ttl_seconds: float | None = None) -> bool:
        payload = serialize(document)
        created = self._call(
            lambda: self._redis.set(self._doc(collection, key), payload, nx=True, px=_millis(ttl_seconds))
        )
        return bool(created)

    def increment(self, collection: str, key: str, amount: int = 1, ttl_seconds: float | None = None) -> int:
        millis = _millis(ttl_seconds) or 0
        result = self._call(lambda: self._increment(keys=[self._counter(collection, key)], args=[amount, millis]))
        return int(result)

    def find(self, collection: str, predicate: Callable[[Document], bool] | None = None) -> list[Document]:
        def load() -> list[Document]:
            found: list[Document] = []
            batch: list[str] = []

            def flush() -> None:
                if not batch:
                    return
                values: Any = self._redis.mget(batch)
                for raw in values:
                    if raw is None:  # expired between SCAN and MGET
                        continue
                    value = json.loads(raw)
                    if isinstance(value, dict) and (predicate is None or predicate(value)):
                        found.append(value)
                batch.clear()

            for name in self._scan(collection):
                batch.append(name)
                if len(batch) >= _SCAN_COUNT:
                    flush()
            flush()
            return found

        return self._call(load)

    def keys(self, collection: str) -> list[str]:
        head = len(f"{self._prefix}:doc:{collection}:")
        return self._call(lambda: sorted(name[head:] for name in self._scan(collection)))

    def add_to_set(self, collection: str, key: str, member: str) -> bool:
        return bool(self._call(lambda: self._redis.sadd(self._set(collection, key), member)))

    def remove_from_set(self, collection: str, key: str, member: str) -> bool:
        return bool(self._call(lambda: self._redis.srem(self._set(collection, key), member)))

    def set_members(self, collection: str, key: str) -> set[str]:
        members: Any = self._call(lambda: self._redis.smembers(self._set(collection, key)))
        return {str(member) for member in members}

    def purge_expired(self) -> int:
        return 0  # Redis expires keys by itself

    def ping(self) -> bool:
        try:
            return bool(self._redis.ping())
        except RedisError:
            return False

    def close(self) -> None:
        self._redis.close()  # type: ignore[no-untyped-call]
