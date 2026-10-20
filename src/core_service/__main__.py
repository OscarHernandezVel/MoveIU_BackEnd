"""Run the service: ``python -m core_service`` (configuration comes from CORE_* variables).

``python -m core_service migrate`` only applies the schema (and the TimescaleDB / PostGIS configuration) and exits:
the deploy step of serverless setups, where the application itself starts with ``CORE_DB_MIGRATE_ON_START=false``."""

from __future__ import annotations

import os
import sys

import uvicorn

from core_service.api import create_app
from core_service.config import Settings
from core_service.container import ApplicationContext
from core_service.logging_config import configure_logging


def migrate() -> None:
    """Apply the schema with the migration role (``CORE_DATABASE_MIGRATION_URL``) when there is one."""
    from core_service.clock import SystemClock
    from core_service.persistence.relational import Database, Migrator, SpatialManager, TimescaleManager

    configure_logging(os.environ.get("CORE_LOG_LEVEL", "INFO"))
    settings = Settings.from_env()
    db = settings.database
    owner = Database(
        db.migration_url or db.url,
        schema=db.schema or None,
        min_size=1,
        max_size=1,
        statement_timeout_ms=max(db.statement_timeout_ms, 120_000),
        lock_timeout_ms=60_000,
    )
    try:
        applied = Migrator(owner, SystemClock()).migrate()
        timescale = TimescaleManager(owner).configure(db.telemetry_retention_days, db.telemetry_compress_after_days)
        spatial = SpatialManager(owner).configure()
    finally:
        owner.close()
    print(
        f"Migraciones aplicadas: {applied or 'ninguna pendiente'} | series de tiempo: {timescale.mode} | "
        f"geometria: {spatial.mode}"
    )


def main() -> None:
    if len(sys.argv) > 1 and sys.argv[1] == "migrate":
        migrate()
        return
    configure_logging(os.environ.get("CORE_LOG_LEVEL", "INFO"))
    settings = Settings.from_env()
    seed = os.environ.get("CORE_SEED_DEMO", "false").strip().lower() in ("1", "true", "yes")
    context = ApplicationContext.create(settings, seed_demo=seed)
    run_jobs = os.environ.get("CORE_RUN_JOBS", "true").strip().lower() in ("1", "true", "yes")
    app = create_app(context, run_jobs=run_jobs)
    uvicorn.run(
        app,
        host=os.environ.get("CORE_HOST", "127.0.0.1"),
        port=int(os.environ.get("CORE_PORT") or os.environ.get("PORT") or "8080"),  # PORT: Vercel, Render, Heroku...
        log_config=None,
    )


if __name__ == "__main__":
    main()
