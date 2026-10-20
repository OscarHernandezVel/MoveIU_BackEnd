"""Guest access, optional registration, login, refresh and logout."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Response

from core_service.api.dependencies import get_context, require_device
from core_service.api.schemas import GuestIn, LoginIn, PushTokenIn, RefreshIn, RegisterIn
from core_service.auth import TokenPair
from core_service.container import ApplicationContext
from core_service.errors import NotFoundError

router = APIRouter(prefix="/auth", tags=["auth"])


def token_response(pair: TokenPair) -> dict[str, Any]:
    return {
        "access_token": pair.access_token,
        "refresh_token": pair.refresh_token,
        "token_type": "Bearer",
        "expires_at": pair.access_expires_at,
        "role": pair.role.value,
    }


@router.post("/guest", summary="Use the app without an account")
def guest(body: GuestIn, context: ApplicationContext = Depends(get_context)) -> dict[str, Any]:
    return token_response(context.auth.guest(body.device_id, body.platform, body.push_token))


@router.post("/register", status_code=201, summary="Create an optional account")
def register(body: RegisterIn, context: ApplicationContext = Depends(get_context)) -> dict[str, Any]:
    return token_response(context.auth.register(body.name, body.email, body.phone, body.password, body.device_id))


@router.post("/login", summary="Sign in")
def login(body: LoginIn, context: ApplicationContext = Depends(get_context)) -> dict[str, Any]:
    return token_response(context.auth.login(body.email, body.password, body.device_id))


@router.post("/refresh", summary="Exchange a refresh token (single use) for a new pair")
def refresh(body: RefreshIn, context: ApplicationContext = Depends(get_context)) -> dict[str, Any]:
    return token_response(context.auth.refresh(body.refresh_token))


@router.post("/logout", status_code=204, summary="Revoke a refresh token")
def logout(body: RefreshIn, context: ApplicationContext = Depends(get_context)) -> Response:
    context.auth.logout(body.refresh_token)
    return Response(status_code=204)


@router.put("/device/push-token", status_code=204, summary="Update the push token of this phone")
def update_push_token(
    body: PushTokenIn, device_id: str = Depends(require_device), context: ApplicationContext = Depends(get_context)
) -> Response:
    if not context.devices.update_push_token(device_id, body.push_token, context.clock.now()):
        raise NotFoundError("device_not_registered")
    return Response(status_code=204)
