"""PostgreSQL access: connection pool, nestable transactions and failure mapping.

* A :class:`psycopg_pool.ConnectionPool` serves every thread. Connections are validated when
  they are handed out, so after a fail-over (a new primary behind the same address) dead
  sockets are discarded instead of surfacing as errors.
* ``transaction()`` pins one connection to the calling thread. Nested blocks become
  *savepoints*: an error inside an inner block undoes only that block.
* Every statement is parameterised (``%s``): user input is never concatenated into SQL.
* Server-side guard rails are set per connection: ``statement_timeout`` (no query can hold a
  worker forever), ``lock_timeout`` and ``idle_in_transaction_session_timeout`` (a crashed
  client cannot keep locks).
* Infrastructure failures (server down, pool exhausted) are raised as
  :class:`ServiceUnavailableError` so the API answers 503 instead of leaking driver errors.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from typing import Any, TypeVar

import psycopg
from psycopg import errors as pg_errors
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool, PoolTimeout

from core_service.errors import ServiceUnavailableError

log = logging.getLogger(__name__)

Row = dict[str, Any]
T = TypeVar("T")

#: Re-exported so repositories do not import the driver directly.
IntegrityError = pg_errors.IntegrityError
UniqueViolation = pg_errors.UniqueViolation

#: The connection or the server is (briefly) unavailable: reads may be retried.
_TRANSIENT = (psycopg.OperationalError, PoolTimeout)
#: The server answered, but refused or aborted the statement (slow query, lock wait, deadlock).
#: These are subclasses of ``OperationalError`` too, but repeating the statement would only make things worse.
_REFUSED = (
    pg_errors.QueryCanceled,
    pg_errors.LockNotAvailable,
    pg_errors.DeadlockDetected,
    pg_errors.SerializationFailure,
)


class Database:
    def __init__(
        self,
        dsn: str,
        *,
        schema: str | None = None,
        min_size: int = 1,
        max_size: int = 10,
        connect_timeout_seconds: float = 5.0,
        pool_timeout_seconds: float = 5.0,
        statement_timeout_ms: int = 10_000,
        lock_timeout_ms: int = 5_000,
        idle_in_transaction_timeout_ms: int = 15_000,
        application_name: str = "core-service",
        read_retries: int = 2,
        server_side_prepare: bool = True,
        startup_options: bool = True,
    ) -> None:
        if min_size < 1 or max_size < min_size:
            raise ValueError("pool sizes must satisfy 1 <= min_size <= max_size")
        options = (
            f"-c statement_timeout={int(statement_timeout_ms)} "
            f"-c lock_timeout={int(lock_timeout_ms)} "
            f"-c idle_in_transaction_session_timeout={int(idle_in_transaction_timeout_ms)}"
        )
        if schema is not None:
            if not schema.replace("_", "").isalnum():
                raise ValueError("schema must be alphanumeric")
            if not startup_options:
                raise ValueError("a schema needs start-up options (set search_path on the role instead)")
            options += f" -c search_path={schema},public"
        self._schema = schema
        self._read_retries = max(0, read_retries)
        self._local = threading.local()
        connection_kwargs: dict[str, Any] = {
            "autocommit": True,
            "row_factory": dict_row,
            "connect_timeout": max(1, int(connect_timeout_seconds)),
            "application_name": application_name,
        }
        if startup_options:
            connection_kwargs["options"] = options
        if not server_side_prepare:
            connection_kwargs["prepare_threshold"] = None  # transaction poolers do not keep prepared statements
        self._pool = ConnectionPool(
            conninfo=dsn,
            kwargs=connection_kwargs,
            min_size=min_size,
            max_size=max_size,
            timeout=pool_timeout_seconds,
            check=ConnectionPool.check_connection,
            max_lifetime=1800.0,
            open=False,
        )
        try:
            self._pool.open(wait=True, timeout=max(pool_timeout_seconds, connect_timeout_seconds) + 1.0)
        except _TRANSIENT as error:
            self._pool.close()
            raise ServiceUnavailableError() from error

    # ------------------------------------------------------------------ plumbing
    @property
    def schema(self) -> str | None:
        return self._schema

    def _active(self) -> psycopg.Connection[Any] | None:
        connection: psycopg.Connection[Any] | None = getattr(self._local, "connection", None)
        return connection

    def _run(self, work: Callable[[psycopg.Connection[Any]], T], *, retry: bool) -> T:
        pinned = self._active()
        if pinned is not None:
            return self._guard(work, pinned)  # inside a transaction: never retry a partial unit of work
        attempts = 1 + (self._read_retries if retry else 0)
        for attempt in range(1, attempts + 1):
            try:
                with self._pool.connection() as connection:
                    return work(connection)
            except _REFUSED as error:
                log.error("Database refused the statement: %s", type(error).__name__)
                raise ServiceUnavailableError() from error
            except _TRANSIENT as error:
                if attempt == attempts:
                    log.error("Database unavailable: %s", error)
                    raise ServiceUnavailableError() from error
                time.sleep(0.05 * attempt)
        raise AssertionError("unreachable")  # pragma: no cover

    @staticmethod
    def _guard(work: Callable[[psycopg.Connection[Any]], T], connection: psycopg.Connection[Any]) -> T:
        try:
            return work(connection)
        except (*_REFUSED, *_TRANSIENT) as error:
            raise ServiceUnavailableError() from error

    # -------------------------------------------------------------- transactions
    @contextmanager
    def transaction(self) -> Iterator[None]:
        """Atomic block. Nested blocks are savepoints of the outermost transaction."""
        pinned = self._active()
        if pinned is not None:
            with pinned.transaction():
                yield
            return
        try:
            with self._pool.connection() as connection:
                self._local.connection = connection
                try:
                    with connection.transaction():
                        yield
                finally:
                    self._local.connection = None
        except (*_REFUSED, *_TRANSIENT) as error:
            raise ServiceUnavailableError() from error

    # ------------------------------------------------------------------ statements
    def execute(self, sql: str, params: Sequence[Any] = ()) -> int:
        """Run a write statement; returns the number of affected rows."""
        return self._run(lambda c: c.execute(sql, params).rowcount, retry=False)

    def query_all(self, sql: str, params: Sequence[Any] = ()) -> list[Row]:
        return self._run(lambda c: c.execute(sql, params).fetchall(), retry=True)

    def query_one(self, sql: str, params: Sequence[Any] = ()) -> Row | None:
        return self._run(lambda c: c.execute(sql, params).fetchone(), retry=True)

    def write_returning(self, sql: str, params: Sequence[Any] = ()) -> list[Row]:
        """Write statement with ``RETURNING``: atomic read-and-modify without a read/write race."""
        return self._run(lambda c: c.execute(sql, params).fetchall(), retry=False)

    # ---------------------------------------------------------------------- health
    def ping(self) -> bool:
        try:
            row = self.query_one("SELECT 1 AS ok")
        except ServiceUnavailableError:
            return False
        return row is not None and row["ok"] == 1

    def pool_stats(self) -> dict[str, int]:
        return {k: int(v) for k, v in self._pool.get_stats().items()}

    def close(self) -> None:
        self._pool.close()
