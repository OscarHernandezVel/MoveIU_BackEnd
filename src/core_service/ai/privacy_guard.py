"""Keeps personal data out of everything sent to an AI model.

The AI only ever receives numeric fleet data. This guard is a second line of defence: it scans
any payload for forbidden keys and for values that look like e-mails or phone numbers, and
raises before the payload can leave the process.
"""

from __future__ import annotations

import re
from typing import Any

from core_service.errors import PrivacyViolationError

FORBIDDEN_KEYS = frozenset(
    {
        "email",
        "phone",
        "name",
        "password",
        "token",
        "push_token",
        "device_id",
        "user_id",
        "auditor_id",
        "address",
        "image",
        "frame",
        "face",
    }
)
_EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
_PHONE = re.compile(r"(?<!\d)\+?\d{9,15}(?!\d)")
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")


class PrivacyGuard:
    def assert_clean(self, payload: Any, path: str = "$") -> None:
        if isinstance(payload, dict):
            for key, value in payload.items():
                if str(key).lower() in FORBIDDEN_KEYS:
                    raise PrivacyViolationError(details=[f"{path}.{key}: forbidden field"])
                self.assert_clean(value, f"{path}.{key}")
        elif isinstance(payload, (list, tuple)):
            for index, item in enumerate(payload):
                self.assert_clean(item, f"{path}[{index}]")
        elif isinstance(payload, str) and (_EMAIL.search(payload) or _PHONE.search(payload)):
            raise PrivacyViolationError(details=[f"{path}: looks like personal data"])

    @staticmethod
    def sanitize_text(text: str, max_length: int = 500) -> str:
        """Strip control characters, collapse whitespace and truncate (for free text from users or the model)."""
        cleaned = " ".join(_CONTROL.sub(" ", text).split())
        return cleaned[:max_length]
