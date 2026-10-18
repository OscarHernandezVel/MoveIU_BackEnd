"""Builds the Redis client used by the service (standalone, TLS or Sentinel).

* **Standalone / managed Redis**: ``CORE_REDIS_URL=rediss://user:password@host:6379/0``. Use the
  ``rediss://`` scheme (TLS) and an ACL user in production; the settings validator enforces it.
* **High availability with Sentinel**: ``CORE_REDIS_SENTINELS=host1:26379,host2:26379`` plus
  ``CORE_REDIS_SENTINEL_MASTER=mymaster``. The client asks the Sentinels for the current primary
  and re-discovers it after a fail-over, so no operator action is needed.

Every socket has a timeout (a hung Redis must not hang a request) and idle connections are
health-checked, so broken sockets are replaced instead of failing user requests.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import redis
from redis.backoff import ExponentialBackoff
from redis.exceptions import BusyLoadingError
from redis.exceptions import ConnectionError as RedisConnectionError
from redis.exceptions import TimeoutError as RedisTimeout
from redis.retry import Retry
from redis.sentinel import Sentinel


@dataclass(frozen=True)
class RedisOptions:
    url: str = ""
    sentinels: tuple[tuple[str, int], ...] = ()
    sentinel_master: str = "mymaster"
    sentinel_password: str = ""
    password: str = ""
    username: str = ""
    tls_ca_path: str = ""
    max_connections: int = 50
    socket_timeout_seconds: float = 2.0
    connect_timeout_seconds: float = 2.0
    health_check_interval_seconds: int = 15


def parse_sentinels(text: str) -> tuple[tuple[str, int], ...]:
    """``"a:26379,b:26379"`` -> ``(("a", 26379), ("b", 26379))``."""
    result: list[tuple[str, int]] = []
    for part in (piece.strip() for piece in text.split(",")):
        if not part:
            continue
        host, _, port = part.rpartition(":")
        if not host or not port.isdigit():
            raise ValueError(f"invalid sentinel address: {part!r} (expected host:port)")
        result.append((host, int(port)))
    return tuple(result)


def _common(options: RedisOptions) -> dict[str, Any]:
    retry = Retry(ExponentialBackoff(cap=0.5, base=0.05), retries=3)
    common: dict[str, Any] = {
        "decode_responses": True,
        "socket_timeout": options.socket_timeout_seconds,
        "socket_connect_timeout": options.connect_timeout_seconds,
        "health_check_interval": options.health_check_interval_seconds,
        "retry": retry,
        "retry_on_error": [RedisConnectionError, RedisTimeout, BusyLoadingError],
        "max_connections": options.max_connections,
    }
    if options.password:
        common["password"] = options.password
    if options.username:
        common["username"] = options.username
    return common


def build_redis_client(options: RedisOptions) -> redis.Redis:
    common = _common(options)
    if options.sentinels:
        sentinel_kwargs: dict[str, Any] = {
            "socket_timeout": options.socket_timeout_seconds,
            "socket_connect_timeout": options.connect_timeout_seconds,
        }
        if options.sentinel_password:
            sentinel_kwargs["password"] = options.sentinel_password
        if options.tls_ca_path:
            common.update(ssl=True, ssl_ca_certs=options.tls_ca_path)
            sentinel_kwargs.update(ssl=True, ssl_ca_certs=options.tls_ca_path)
        sentinel = Sentinel(list(options.sentinels), sentinel_kwargs=sentinel_kwargs, **common)  # type: ignore[no-untyped-call]
        client: redis.Redis = sentinel.master_for(options.sentinel_master)  # type: ignore[no-untyped-call]
        return client
    if not options.url:
        raise ValueError("a Redis URL or a list of Sentinels is required")
    if options.tls_ca_path:
        common["ssl_ca_certs"] = options.tls_ca_path
    return redis.Redis.from_url(options.url, **common)


def describe(options: RedisOptions) -> str:
    """Safe text for logs: never includes credentials."""
    if options.sentinels:
        return f"sentinel:{options.sentinel_master}@{','.join(f'{h}:{p}' for h, p in options.sentinels)}"
    return options.url.split("@")[-1] if options.url else "unconfigured"
