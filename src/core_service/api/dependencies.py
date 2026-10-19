"""FastAPI dependencies: application context, locale, authentication and authorisation."""

from __future__ import annotations

import hmac
from collections.abc import Callable

from fastapi import Depends, Request

from core_service.container import ApplicationContext
from core_service.errors import AuthenticationError, AuthorizationError, ServiceUnavailableError, ValidationError
from core_service.models import Principal, Role


def get_context(request: Request) -> ApplicationContext:
    context: ApplicationContext = request.app.state.context
    return context


def get_locale(request: Request, context: ApplicationContext = Depends(get_context)) -> str:
    return context.messages.normalize_locale(request.headers.get("accept-language"))


def get_principal(request: Request, context: ApplicationContext = Depends(get_context)) -> Principal:
    header = request.headers.get("authorization", "")
    scheme, _, token = header.partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        raise AuthenticationError("authentication_failed")
    return context.tokens.verify_access(token.strip())


def require_roles(*roles: Role) -> Callable[[Principal], Principal]:
    allowed = frozenset(roles)

    def dependency(principal: Principal = Depends(get_principal)) -> Principal:
        if principal.role not in allowed:
            raise AuthorizationError("forbidden")
        return principal

    return dependency


def require_device(principal: Principal = Depends(get_principal)) -> str:
    """Alerts and notifications belong to a phone: the token must carry its device id."""
    if not principal.device_id:
        raise ValidationError("device_not_registered")
    return principal.device_id


def require_ingest_key(request: Request, context: ApplicationContext = Depends(get_context)) -> None:
    expected = context.settings.security.ingest_key
    if not expected:
        raise ServiceUnavailableError("service_unavailable")  # ingestion disabled until a key is configured
    provided = request.headers.get("x-ingest-key", "")
    if not hmac.compare_digest(provided.encode("utf-8"), expected.encode("utf-8")):
        raise AuthenticationError("authentication_failed")


def require_cron_secret(request: Request, context: ApplicationContext = Depends(get_context)) -> None:
    """Scheduler access: ``Authorization: Bearer <CORE_CRON_SECRET>`` (the same scheme as Vercel Cron)."""
    expected = context.settings.security.cron_secret
    if not expected:
        raise ServiceUnavailableError("service_unavailable")  # scheduled jobs are disabled until a secret is set
    provided = request.headers.get("authorization", "")
    if not hmac.compare_digest(provided.encode("utf-8"), f"Bearer {expected}".encode()):
        raise AuthenticationError("authentication_failed")
