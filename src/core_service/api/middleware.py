"""HTTP middleware: body-size limit, request id, security headers and rate limiting."""

from __future__ import annotations

import hmac
import re
import uuid
from typing import Any

from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import Response
from starlette.types import ASGIApp, Receive, Scope, Send

from core_service.api.errors import error_response

_REQUEST_ID = re.compile(r"^[A-Za-z0-9-]{8,64}$")
_EXEMPT_FROM_RATE_LIMIT = ("/health/",)
#: Readings of one bus (``/api/v1/internal/buses/{bus_id}/telemetry``).
_BUS_TELEMETRY = re.compile(r"^/api/v1/internal/buses/([A-Za-z0-9-]{1,20})/telemetry$")


class BodySizeLimitMiddleware:
    """Rejects bodies over ``max_bytes`` - by header when declared, by counting when streamed."""

    def __init__(self, app: ASGIApp, max_bytes: int) -> None:
        self._app = app
        self._max = max_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return
        declared = dict(scope["headers"]).get(b"content-length")
        if declared is not None and declared.isdigit() and int(declared) > self._max:
            await self._reject(scope, receive, send)
            return
        received = 0
        too_large = False

        async def limited_receive() -> Any:
            nonlocal received, too_large
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > self._max:
                    too_large = True
                    return {"type": "http.request", "body": b"", "more_body": False}
            return message

        started = False

        async def guarded_send(message: Any) -> None:
            nonlocal started
            if too_large and not started:
                started = True
                await self._reject(scope, receive, send)
                return
            if not too_large:
                await send(message)

        await self._app(scope, limited_receive, guarded_send)

    @staticmethod
    async def _reject(scope: Scope, receive: Receive, send: Send) -> None:
        request = Request(scope, receive)
        response = error_response(request, 413, "payload_too_large")
        await response(scope, receive, send)


class RequestContextMiddleware(BaseHTTPMiddleware):
    def __init__(self, app: ASGIApp, production: bool) -> None:
        super().__init__(app)
        self._production = production

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        incoming = request.headers.get("x-request-id", "")
        request.state.request_id = incoming if _REQUEST_ID.match(incoming) else str(uuid.uuid4())

        context = request.app.state.context
        if not request.url.path.startswith(_EXEMPT_FROM_RATE_LIMIT) and not request.url.path.endswith(
            ("/health/live", "/health/ready")
        ):
            decision = context.rate_limiter.check(self._limit_key(request, context.settings.security))
            if not decision.allowed:
                context.metrics.increment("http_rate_limited")
                response: Response = error_response(
                    request,
                    429,
                    "rate_limited",
                    headers={"Retry-After": str(max(1, int(decision.retry_after_seconds + 0.999)))},
                )
                return self._decorate(request, response)
        response = await call_next(request)
        return self._decorate(request, response)

    @classmethod
    def _limit_key(cls, request: Request, security: Any) -> str:
        """Bucket of the request limiter.

        Every bus reports through the same gateway (the MQTT/Kafka bridge), so limiting readings per IP would
        throttle the whole fleet at once (v7 load test: 50 buses every 2 s). Readings that carry the valid
        ingest key are limited *per bus* instead; everything else, including readings with a wrong key, stays
        limited per client address."""
        match = _BUS_TELEMETRY.match(request.url.path)
        if match and security.ingest_key:
            provided = request.headers.get("x-ingest-key", "")
            if hmac.compare_digest(provided.encode("utf-8"), security.ingest_key.encode("utf-8")):
                return f"bus:{match.group(1)}"
        return cls._client_key(request, security.trust_proxy)

    @staticmethod
    def _client_key(request: Request, trust_proxy: bool) -> str:
        if trust_proxy:
            forwarded = request.headers.get("x-forwarded-for", "")
            if forwarded:
                return forwarded.split(",")[0].strip()[:64]
        return request.client.host if request.client else "unknown"

    def _decorate(self, request: Request, response: Response) -> Response:
        response.headers["X-Request-ID"] = request.state.request_id
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Content-Security-Policy"] = "default-src 'none'; frame-ancestors 'none'"
        response.headers["Cache-Control"] = "no-store"
        if self._production:
            response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
        return response
