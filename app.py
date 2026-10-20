"""Entrada del backend en Vercel (runtime de Python, FastAPI): Vercel busca un objeto ``app`` en este archivo.

El mismo codigo corre en un contenedor (``python -m core_service``) y en funciones de Vercel; lo que cambia es el
arranque. En funciones no hay un proceso siempre encendido, asi que aqui:

* no se aplican migraciones al arrancar (se aplican en el despliegue: ``python -m core_service migrate``);
* no se lanzan los hilos de tareas periodicas (los llama un cron: ``GET /api/v1/internal/jobs/run``);
* el pool de conexiones es pequeno (cada instancia abre las suyas) y el estado compartido vive en Valkey/Redis.

Cada valor puede cambiarse con su variable CORE_*; aqui solo se dan valores por defecto.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

for name, value in {
    "CORE_DB_MIGRATE_ON_START": "false",
    "CORE_DB_POOL_MIN": "1",
    "CORE_DB_POOL_MAX": "3",
    "CORE_REDIS_MAX_CONNECTIONS": "10",
    "CORE_TRUST_PROXY": "true",  # Vercel pone la IP real en X-Forwarded-For
}.items():
    os.environ.setdefault(name, value)

from core_service.api import create_app  # noqa: E402
from core_service.config import Settings  # noqa: E402
from core_service.container import ApplicationContext  # noqa: E402
from core_service.logging_config import configure_logging  # noqa: E402

configure_logging(os.environ.get("CORE_LOG_LEVEL", "INFO"))
_settings = Settings.from_env()
_context = ApplicationContext.create(
    _settings, seed_demo=os.environ.get("CORE_SEED_DEMO", "false").strip().lower() in ("1", "true", "yes")
)
app = create_app(_context, run_jobs=False)
