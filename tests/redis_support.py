"""Redis for the tests: fakeredis by default, a real server when ``CORE_TEST_REDIS_URL`` is set.

The fake speaks the real protocol, including Lua scripts and pub/sub, so the same tests run against
both. Run them against a real Redis before a release (``docker compose -f docker-compose.dev.yml up -d``).
"""

from __future__ import annotations

import os
from typing import Any

import fakeredis
import redis

_ENV = "CORE_TEST_REDIS_URL"


def real_redis_url() -> str | None:
    return os.environ.get(_ENV) or None


def new_client(kind: str) -> Any:
    if kind == "real":
        client = redis.Redis.from_url(os.environ[_ENV], decode_responses=True)
        client.flushdb()
        return client
    return fakeredis.FakeRedis(server=fakeredis.FakeServer(), decode_responses=True)
