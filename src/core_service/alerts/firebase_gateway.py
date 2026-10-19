"""Firebase Cloud Messaging adapter (Android directly, iOS through APNs).

Install the optional dependency with ``pip install "core-service[push]"``. NOTE: this adapter is
written against the ``firebase-admin`` API but has not been exercised against a real Firebase
project in this repository's tests; verify it with your credentials before production use.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from core_service.alerts.channels import ChannelDeliveryError, PushGateway, PushPayload


class FirebasePushGateway(PushGateway):
    def __init__(self, credentials_path: str) -> None:
        import firebase_admin  # lazy: only needed when this gateway is enabled
        from firebase_admin import credentials, messaging

        self._messaging: Any = messaging
        self._app = firebase_admin.initialize_app(credentials.Certificate(credentials_path), name="capacidad-bus")

    def send(self, payload: PushPayload) -> None:
        m = self._messaging
        message = m.Message(
            token=payload.token,
            notification=m.Notification(title=payload.title, body=payload.body),
            data=payload.data,
            android=m.AndroidConfig(
                priority="high" if payload.priority == "high" else "normal",
                ttl=timedelta(seconds=payload.ttl_seconds),
                notification=m.AndroidNotification(
                    channel_id=payload.channel_id,
                    sound=payload.sound,
                    vibrate_timings_millis=list(payload.vibrate_pattern),
                ),
            ),
            apns=m.APNSConfig(
                headers={"apns-priority": "10" if payload.priority == "high" else "5"},
                payload=m.APNSPayload(aps=m.Aps(sound=f"{payload.sound}.caf")),
            ),
        )
        try:
            m.send(message, app=self._app)
        except (m.UnregisteredError, m.SenderIdMismatchError) as error:
            raise ChannelDeliveryError(f"push token rejected: {error}", retryable=False) from error
        except Exception as error:  # noqa: BLE001
            raise ChannelDeliveryError(f"push provider error: {error}") from error
