"""FastAPI application factory."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from core_service import __version__
from core_service.api.errors import install_error_handlers
from core_service.api.middleware import BodySizeLimitMiddleware, RequestContextMiddleware
from core_service.api.routers import admin, ai, alerts, audits, auth, catalog, health, ingestion, jobs, realtime
from core_service.container import ApplicationContext

API_PREFIX = "/api/v1"


def create_app(context: ApplicationContext, *, run_jobs: bool = False) -> FastAPI:
    """Build the HTTP layer around an already wired :class:`ApplicationContext`."""

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        if run_jobs:
            context.start_background()
        try:
            yield
        finally:
            if context.realtime is not None:
                await context.realtime.shutdown()
            if run_jobs:
                context.stop_background()

    production = context.settings.is_production
    app = FastAPI(
        title="Bus net-capacity core service",
        version=__version__,
        lifespan=lifespan,
        docs_url=None if production else "/docs",
        redoc_url=None,
        openapi_url=None if production else "/openapi.json",
    )
    app.state.context = context
    install_error_handlers(app)

    for router in (
        auth.router,
        catalog.router,
        alerts.router,
        audits.router,
        ingestion.router,
        jobs.router,
        ai.router,
        admin.router,
    ):
        app.include_router(router, prefix=API_PREFIX)
    app.include_router(health.router, prefix=API_PREFIX)
    app.include_router(realtime.router)  # WS /ws/v1/buses/stream (v7 path, outside /api/v1)

    security = context.settings.security
    if security.cors_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=list(security.cors_origins),
            allow_methods=["GET", "POST", "PUT", "DELETE"],
            allow_headers=["Authorization", "Content-Type", "Accept-Language", "X-Request-ID"],
            max_age=600,
        )
    app.add_middleware(RequestContextMiddleware, production=production)
    app.add_middleware(BodySizeLimitMiddleware, max_bytes=security.max_body_bytes)
    return app
