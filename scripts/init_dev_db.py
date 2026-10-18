"""Crea (o actualiza) el esquema de la base de datos de desarrollo y muestra qué quedó activo.

Uso, con la base levantada por ``docker compose -f docker-compose.dev.yml up -d``::

    python scripts/init_dev_db.py
    python scripts/init_dev_db.py --url postgresql://core:core@localhost:5432/core

Es idempotente: se puede repetir. Aplica las migraciones (tablas del documento v7), activa TimescaleDB y
PostGIS si el servidor los trae, y lista tablas, extensiones y versión del esquema. Termina con código 1 si
falla la conexión o una migración.
"""

from __future__ import annotations

import argparse
import os
import sys

import psycopg

from core_service.clock import SystemClock
from core_service.persistence.relational import Database, Migrator
from core_service.persistence.relational.spatial import SpatialManager
from core_service.persistence.relational.timescale import TimescaleManager

DEFAULT_URL = "postgresql://core:core@localhost:5432/core"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--url", default=os.environ.get("CORE_DATABASE_URL", DEFAULT_URL))
    parser.add_argument("--schema", default=os.environ.get("CORE_DATABASE_SCHEMA", ""))
    args = parser.parse_args(argv)
    shown = args.url.split("@")[-1]
    print(f"Conectando a {shown} ...")
    try:
        database = Database(
            args.url,
            schema=args.schema or None,
            min_size=1,
            max_size=2,
            lock_timeout_ms=60_000,
            connect_timeout_seconds=15.0,
            pool_timeout_seconds=15.0,
        )
    except Exception as error:  # noqa: BLE001 - message for the operator
        print(f"No se pudo conectar: {type(error).__name__}. ¿Está corriendo el contenedor? (docker compose ps)")
        return 1
    try:
        Migrator(database, SystemClock()).migrate()
        timescale = TimescaleManager(database).configure()
        spatial = SpatialManager(database).configure()
        version = database.query_one("SELECT max(version) AS v FROM schema_migrations")
        tables = database.query_all(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema = current_schema() AND table_type = 'BASE TABLE' ORDER BY table_name"
        )
        extensions = database.query_all("SELECT extname FROM pg_extension ORDER BY extname")
    except (psycopg.Error, OSError) as error:
        print(f"Falló la creación del esquema: {type(error).__name__}: {error}")
        return 1
    finally:
        database.close()
    print(f"Esquema en la versión {version['v'] if version else '?'}")
    print(f"Tablas ({len(tables)}): " + ", ".join(row["table_name"] for row in tables))
    print("Extensiones: " + ", ".join(row["extname"] for row in extensions))
    print(f"TimescaleDB: {'activo' if timescale.enabled else 'NO activo (tabla normal)'}")
    print(f"PostGIS:     {'activo' if spatial.enabled else 'NO activo (solo lat/lng)'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
