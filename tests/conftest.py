"""Shared pytest fixtures: a real PostgreSQL (schema per test) and Redis (fake or real)."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest

import pg
import redis_support


@pytest.fixture(scope="session", autouse=True)
def _postgres_server() -> Iterator[None]:
    try:
        pg.ensure_server()
    except pg.NoPostgresError as error:
        pytest.exit(str(error), returncode=2)
    yield
    pg.shutdown()


@pytest.fixture(autouse=True)
def _release_test_resources() -> Iterator[None]:
    yield
    pg.release_all()


@pytest.fixture(params=["fake", *(["real"] if redis_support.real_redis_url() else [])])
def redis_client(request: pytest.FixtureRequest) -> Iterator[Any]:
    client = redis_support.new_client(request.param)
    yield client
    client.close()
