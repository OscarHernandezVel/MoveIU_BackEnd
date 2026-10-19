"""Maps domain errors to HTTP responses with localised, non-technical messages."""

from __future__ import annotations

import logging
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from core_service.errors import (
    AuthenticationError,
    AuthorizationError,
    ConflictError,
    DomainError,
    NotFoundError,
    PrivacyViolationError,
    RateLimitedError,
    ServiceUnavailableError,
    ValidationError,
)

log = logging.getLogger(__name__)

_STATUS: tuple[tuple[type[DomainError], int], ...] = (
    (ValidationError, 422),
    (PrivacyViolationError, 422),
    (NotFoundError, 404),
    (ConflictError, 409),
    (AuthenticationError, 401),
    (AuthorizationError, 403),
    (RateLimitedError, 429),
    (ServiceUnavailableError, 503),
)


def status_for(error: DomainError) -> int:
    for error_type, status in _STATUS:
        if isinstance(error, error_type):
            return status
    return 400


def error_body(
    request: Request, code: str, params: dict[str, Any] | None = None, details: list[str] | None = None
) -> dict[str, Any]:
    context = request.app.state.context
    locale = context.messages.normalize_locale(request.headers.get("accept-language"))
    return {
        "error": {
            "code": code,
            "message": context.messages.render(code, locale, **(params or {})),
            "details": details or [],
            "request_id": getattr(request.state, "request_id", None),
        }
    }


def error_response(
    request: Request,
    status: int,
    code: str,
    params: dict[str, Any] | None = None,
    details: list[str] | None = None,
    headers: dict[str, str] | None = None,
) -> JSONResponse:
    return JSONResponse(error_body(request, code, params, details), status_code=status, headers=headers)


def install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(DomainError)
    async def domain_error(request: Request, error: DomainError) -> JSONResponse:
        headers: dict[str, str] = {}
        status = status_for(error)
        if status == 401:
            headers["WWW-Authenticate"] = "Bearer"
        if isinstance(error, RateLimitedError) and "retry_after" in error.params:
            headers["Retry-After"] = str(error.params["retry_after"])
        return error_response(request, status, error.code, error.params, error.details, headers)

    @app.exception_handler(RequestValidationError)
    async def request_validation(request: Request, error: RequestValidationError) -> JSONResponse:
        # Report *where* the problem is, never the submitted value (it may be sensitive).
        details = [f"{'.'.join(str(p) for p in item['loc'])}: {item['type']}" for item in error.errors()[:10]]
        return error_response(request, 422, "validation_error", details=details)

    @app.exception_handler(StarletteHTTPException)
    async def http_error(request: Request, error: StarletteHTTPException) -> JSONResponse:
        code = {404: "not_found", 405: "not_found", 401: "authentication_failed", 403: "forbidden"}.get(
            error.status_code, "internal_error"
        )
        return error_response(request, error.status_code, code)

    @app.exception_handler(Exception)
    async def unexpected(request: Request, error: Exception) -> JSONResponse:
        log.exception("Unhandled error on %s %s", request.method, request.url.path)
        return error_response(request, 500, "internal_error")
