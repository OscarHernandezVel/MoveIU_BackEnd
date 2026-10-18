"""Verifica que las credenciales de PostgreSQL y Redis alcanzan para operar el servicio... y no más.

Uso (con las mismas variables que el servicio):

    CORE_DATABASE_URL=postgresql://core_app:...@host/core?sslmode=verify-full \
    CORE_REDIS_URL=rediss://core:...@host:6379/0 \
    python scripts/verify_data_layer.py

Comprueba:
  * Redis: el usuario puede hacer todo lo que el servicio necesita (documentos, contadores, conjuntos, locks,
    límite de peticiones, pub/sub) y NO puede ejecutar comandos peligrosos ni tocar claves fuera del prefijo.
  * PostgreSQL: el rol puede leer/escribir datos y NO puede crear, alterar ni borrar tablas.

Sale con código 0 si todo es correcto y con 1 si algo no lo es (apto para un paso de despliegue).
"""

from __future__ import annotations

import os
import sys
import time
import uuid
from collections.abc import Callable

import psycopg
from redis.exceptions import ResponseError

from core_service.clock import SystemClock
from core_service.persistence.documents import RedisDocumentStore, RedisOptions, build_redis_client
from core_service.security import RedisSlidingWindowRateLimiter
from core_service.services.keyed_locks import RedisLockManager

PREFIX = os.environ.get("CORE_REDIS_PREFIX", "core")
failures: list[str] = []


def check(name: str, action: Callable[[], object], *, must_fail_with: type[Exception] | None = None) -> None:
    try:
        action()
    except Exception as error:  # noqa: BLE001
        if must_fail_with is not None and isinstance(error, must_fail_with):
            print(f"  ok   {name} (rechazado como corresponde: {type(error).__name__})")
            return
        failures.append(f"{name}: {type(error).__name__}: {error}")
        print(f"  FAIL {name}: {type(error).__name__}: {error}")
        return
    if must_fail_with is not None:
        failures.append(f"{name}: debería haber sido rechazado y no lo fue")
        print(f"  FAIL {name}: debería haber sido rechazado y no lo fue")
        return
    print(f"  ok   {name}")


def verify_redis() -> None:
    url = os.environ.get("CORE_REDIS_URL", "")
    if not url:
        print("Redis: CORE_REDIS_URL no definida, se omite")
        return
    print(f"Redis ({url.split('@')[-1]})")
    client = build_redis_client(RedisOptions(url=url, tls_ca_path=os.environ.get("CORE_REDIS_TLS_CA", "")))
    store = RedisDocumentStore(client, PREFIX)
    tag = uuid.uuid4().hex[:8]
    check("PING", client.ping)
    check(
        "documentos (put/get/delete con TTL)",
        lambda: (
            store.put(f"verify{tag}", "k", {"ok": True}, ttl_seconds=30),
            store.get(f"verify{tag}", "k"),
            store.delete(f"verify{tag}", "k"),
        ),
    )
    check(
        "set_if_absent + increment (Lua)",
        lambda: (
            store.set_if_absent(f"verify{tag}", "n", {"x": 1}, ttl_seconds=30),
            store.increment(f"verify{tag}", "c", ttl_seconds=30),
        ),
    )
    check("conjuntos", lambda: (store.add_to_set(f"verify{tag}", "s", "a"), store.set_members(f"verify{tag}", "s")))
    check("búsqueda con SCAN", lambda: store.keys(f"verify{tag}"))
    check("lock distribuido", lambda: RedisLockManager(client, PREFIX).acquire(f"verify{tag}").__enter__())
    check(
        "límite de peticiones (Lua + ZSET)",
        lambda: RedisSlidingWindowRateLimiter(client, 5, 10.0, SystemClock(), f"{PREFIX}:verify").check(tag),
    )

    def pubsub() -> None:
        sub = client.pubsub(ignore_subscribe_messages=True)
        sub.subscribe(f"{PREFIX}:alert-index")
        client.publish(f"{PREFIX}:alert-index", "{}")
        time.sleep(0.1)
        sub.close()

    check("pub/sub del índice de alertas", pubsub)
    check("FLUSHALL debe estar prohibido", client.flushall, must_fail_with=ResponseError)
    check("CONFIG GET debe estar prohibido", lambda: client.config_get("*"), must_fail_with=ResponseError)
    check("KEYS * debe estar prohibido", lambda: client.keys("*"), must_fail_with=ResponseError)
    check(
        "escribir fuera del prefijo debe estar prohibido",
        lambda: client.set(f"otro:{tag}", "x"),
        must_fail_with=ResponseError,
    )


def verify_postgres() -> None:
    url = os.environ.get("CORE_DATABASE_URL", "")
    if not url:
        print("PostgreSQL: CORE_DATABASE_URL no definida, se omite")
        return
    print(f"PostgreSQL ({url.split('@')[-1].split('?')[0]})")
    with psycopg.connect(url, autocommit=True) as connection:
        check("SELECT", lambda: connection.execute("SELECT count(*) FROM buses").fetchone())
        check(
            "INSERT/DELETE sobre datos",
            lambda: (
                connection.execute(
                    "INSERT INTO stops (stop_id, name, lat, lng) VALUES ('VERIFY', 'verificación', 0, 0)"
                ),
                connection.execute("DELETE FROM stops WHERE stop_id = 'VERIFY'"),
            ),
        )
        check(
            "CREATE TABLE debe estar prohibido",
            lambda: connection.execute("CREATE TABLE verify_tmp (id int)"),
            must_fail_with=psycopg.errors.InsufficientPrivilege,
        )
        check(
            "DROP TABLE debe estar prohibido",
            lambda: connection.execute("DROP TABLE users"),
            must_fail_with=psycopg.errors.InsufficientPrivilege,
        )
        check(
            "TRUNCATE debe estar prohibido",
            lambda: connection.execute("TRUNCATE telemetry_history"),
            must_fail_with=psycopg.errors.InsufficientPrivilege,
        )
        check(
            "ALTER TABLE debe estar prohibido",
            lambda: connection.execute("ALTER TABLE users ADD COLUMN x int"),
            must_fail_with=psycopg.errors.InsufficientPrivilege,
        )
        role = connection.execute("SELECT rolsuper FROM pg_roles WHERE rolname = current_user").fetchone()
        check(
            "el rol no es superusuario",
            lambda: None if role and not role[0] else (_ for _ in ()).throw(RuntimeError("es superusuario")),
        )


def main() -> int:
    verify_redis()
    verify_postgres()
    if failures:
        print(f"\n{len(failures)} comprobación(es) fallida(s):")
        for failure in failures:
            print(f"  - {failure}")
        return 1
    print("\nTodo correcto.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
