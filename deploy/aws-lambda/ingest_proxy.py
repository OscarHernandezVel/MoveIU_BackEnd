"""API Gateway (HTTP API) -> este Lambda -> backend en EC2 (Caddy :8080, dentro de la VPC).

Reenvía tal cual las rutas de ingesta del hardware a bordo (``POST /api/v1/internal/...``). La autenticación
sigue siendo la cabecera ``X-Ingest-Key`` y la valida el backend, que también aplica la lógica de ocupación.
Si el backend está apagado o no responde, contesta 503: el edge guarda las lecturas en su cola SQLite y
reintenta, así que no se pierde nada entre pruebas.
"""

from __future__ import annotations

import base64
import json
import os
import urllib.error
import urllib.request
from typing import Any

BACKEND_URL = os.environ["BACKEND_URL"].rstrip("/")  # p. ej. http://10.0.1.25:8080 (IP privada del EC2)
# Un lote de 500 lecturas tarda ~9 s en el backend; API Gateway corta a los 30 s.
TIMEOUT_S = float(os.environ.get("BACKEND_TIMEOUT_S", "27"))
PREFIX = "/api/v1/internal/"
REQUEST_HEADERS = ("content-type", "x-ingest-key", "x-request-id", "accept-language")
RESPONSE_HEADERS = ("content-type", "retry-after", "x-request-id")


def handler(event: dict[str, Any], context: object) -> dict[str, Any]:
    path = event.get("rawPath", "")
    method = event.get("requestContext", {}).get("http", {}).get("method", "")
    if method != "POST" or not path.startswith(PREFIX) or ".." in path:
        return _error(404, "not_found", "Ruta no disponible")
    body = event.get("body") or ""
    data = base64.b64decode(body) if event.get("isBase64Encoded") else body.encode("utf-8")
    headers = {k: v for k, v in (event.get("headers") or {}).items() if k.lower() in REQUEST_HEADERS}
    request = urllib.request.Request(BACKEND_URL + path, data=data, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_S) as response:  # noqa: S310 - URL fija del entorno
            return _response(response.status, response.headers, response.read())
    except urllib.error.HTTPError as exc:  # 4xx/5xx del backend: se devuelven tal cual
        return _response(exc.code, exc.headers, exc.read())
    except (urllib.error.URLError, OSError):  # apagado, sin red o tiempo agotado
        return _error(503, "backend_unavailable", "El servicio central no responde; reintente más tarde")


def _response(status: int, headers: Any, raw: bytes) -> dict[str, Any]:
    kept = {name: headers.get(name) for name in RESPONSE_HEADERS if headers.get(name)}
    return {"statusCode": status, "headers": kept, "body": raw.decode("utf-8", "replace")}


def _error(status: int, code: str, message: str) -> dict[str, Any]:
    # Mismo formato de error que el backend (docs de la API).
    body = {"error": {"code": code, "message": message, "details": [], "request_id": None}}
    return {"statusCode": status, "headers": {"content-type": "application/json"}, "body": json.dumps(body)}
