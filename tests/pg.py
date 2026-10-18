"""Real PostgreSQL for the tests (no simulator: the SQL that ships is the SQL that is tested).

Where the server comes from, in this order:

1. ``CORE_TEST_DATABASE_URL`` - any reachable PostgreSQL, e.g. the one from ``docker compose -f
   docker-compose.dev.yml up -d`` (``postgresql://core:core@localhost:5432/core_test``). The role
   needs permission to create schemas.
2. A throw-away cluster started from the PostgreSQL binaries found on this machine (``initdb`` and
   ``pg_ctl``), listening only on 127.0.0.1 and deleted at the end. It cannot run as root.

Each test gets its own **schema** (``search_path`` isolates it), so tests never see each other's rows
and can run in any order. Schemas are dropped when the test ends.
"""

from __future__ import annotations

import atexit
import contextlib
import os
import shutil
import socket
import subprocess
import tempfile
import uuid
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import Any

import psycopg

from core_service.clock import Clock
from core_service.config import DatabaseSettings, Settings
from core_service.persistence.relational import Database, Migrator

_ENV = "CORE_TEST_DATABASE_URL"
_state: dict[str, Any] = {"url": None, "cluster": None}
_schemas: list[str] = []
_closers: list[Callable[[], object]] = []


class NoPostgresError(RuntimeError):
    pass


# --------------------------------------------------------------------------- server
def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _find_binaries() -> Path | None:
    found = shutil.which("initdb")
    if found:
        return Path(found).parent
    candidates: list[Path] = []
    for root in (Path("/usr/lib/postgresql"), Path("C:/Program Files/PostgreSQL"), Path("/opt/homebrew/opt")):
        if root.exists():
            candidates.extend(sorted(root.glob("*/bin"), reverse=True))
    candidates.append(Path("/usr/local/pgsql/bin"))
    name = "initdb.exe" if os.name == "nt" else "initdb"
    return next((c for c in candidates if (c / name).exists()), None)


def _start_temporary_cluster() -> str:
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        raise NoPostgresError("PostgreSQL refuses to start as root: set CORE_TEST_DATABASE_URL instead")
    binaries = _find_binaries()
    if binaries is None:
        raise NoPostgresError("no PostgreSQL binaries found")
    suffix = ".exe" if os.name == "nt" else ""
    data = Path(tempfile.mkdtemp(prefix="core-test-pg-"))
    port = _free_port()
    subprocess.run(  # noqa: S603 - fixed argument list, no shell
        [str(binaries / f"initdb{suffix}"), "-D", str(data), "-A", "trust", "-U", "postgres", "-E", "UTF8"],
        check=True,
        capture_output=True,
    )
    options = f"-p {port} -c fsync=off -c listen_addresses=127.0.0.1 -c max_connections=200"
    subprocess.run(  # noqa: S603 - fixed argument list, no shell
        [str(binaries / f"pg_ctl{suffix}"), "-D", str(data), "-o", options, "-l", str(data / "log.txt"), "-w", "start"],
        check=True,
        capture_output=True,
    )
    _state["cluster"] = (binaries / f"pg_ctl{suffix}", data)
    return f"postgresql://postgres@127.0.0.1:{port}/postgres"


def ensure_server() -> str:
    if _state["url"]:
        return str(_state["url"])
    url = os.environ.get(_ENV)
    if not url:
        try:
            url = _start_temporary_cluster()
        except (NoPostgresError, subprocess.CalledProcessError, OSError) as error:
            raise NoPostgresError(
                "Las pruebas necesitan un PostgreSQL real.\n"
                f"  * Define {_ENV}=postgresql://usuario:clave@host:5432/base (por ejemplo con "
                "'docker compose -f docker-compose.dev.yml up -d'), o\n"
                "  * instala PostgreSQL 14+ (initdb/pg_ctl) y ejecuta pytest sin ser root.\n"
                f"Detalle: {error}"
            ) from error
    _state["url"] = url
    return url


def shutdown() -> None:
    cluster = _state.get("cluster")
    if cluster:
        pg_ctl, data = cluster
        subprocess.run(  # noqa: S603 - fixed argument list, no shell
            [str(pg_ctl), "-D", str(data), "-m", "immediate", "stop"], capture_output=True, check=False
        )
        shutil.rmtree(data, ignore_errors=True)
        _state["cluster"] = None
    _state["url"] = None


atexit.register(shutdown)


# --------------------------------------------------------------------------- schemas
def new_schema() -> str:
    schema = "t_" + uuid.uuid4().hex[:14]
    with psycopg.connect(ensure_server(), autocommit=True) as connection:
        connection.execute(f'CREATE SCHEMA "{schema}"')
    _schemas.append(schema)
    return schema


def database_settings(**overrides: Any) -> DatabaseSettings:
    base = DatabaseSettings(url=ensure_server(), schema=new_schema(), pool_min=1, pool_max=4)
    return replace(base, **overrides)


def make_settings(**overrides: Any) -> Settings:
    """``Settings.for_testing`` plus a database in a fresh schema (unless one is given)."""
    if "database" not in overrides:
        overrides["database"] = database_settings()
    return Settings.for_testing(**overrides)


def make_database(clock: Clock | None = None, *, migrate: bool = True) -> Database:
    settings = database_settings()
    database = Database(settings.url, schema=settings.schema, min_size=1, max_size=4)
    _closers.append(database.close)
    if migrate:
        Migrator(database, clock).migrate()
    return database


def on_release(closer: Callable[[], object]) -> None:
    """Run ``closer`` when the current test ends (e.g. ``context.close``)."""
    _closers.append(closer)


def release_all() -> None:
    while _closers:
        closer = _closers.pop()
        with contextlib.suppress(Exception):  # best effort clean-up
            closer()
    if not _schemas:
        return
    with psycopg.connect(ensure_server(), autocommit=True) as connection:
        while _schemas:
            connection.execute(f'DROP SCHEMA IF EXISTS "{_schemas.pop()}" CASCADE')
