"""Password hashing with PBKDF2-HMAC-SHA256 (standard library, salted, constant-time compare).

Stored format: ``pbkdf2_sha256$<iterations>$<salt b64>$<hash b64>``. The iteration count is
stored with each hash, so it can be raised later and old hashes still verify (and can be
detected with :meth:`needs_rehash`).
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import os

ALGORITHM = "pbkdf2_sha256"


def _b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii")


class PasswordHasher:
    def __init__(self, iterations: int = 600_000, salt_bytes: int = 16) -> None:
        if iterations < 1_000:
            raise ValueError("too few iterations")
        self._iterations = iterations
        self._salt_bytes = salt_bytes

    def hash(self, password: str) -> str:
        salt = os.urandom(self._salt_bytes)
        digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, self._iterations)
        return f"{ALGORITHM}${self._iterations}${_b64(salt)}${_b64(digest)}"

    def verify(self, password: str, stored: str) -> bool:
        parsed = self._parse(stored)
        if parsed is None:
            return False
        iterations, salt, expected = parsed
        candidate = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
        return hmac.compare_digest(candidate, expected)

    def needs_rehash(self, stored: str) -> bool:
        parsed = self._parse(stored)
        return parsed is None or parsed[0] < self._iterations

    @staticmethod
    def _parse(stored: str) -> tuple[int, bytes, bytes] | None:
        try:
            algorithm, iterations, salt, digest = stored.split("$")
            if algorithm != ALGORITHM:
                return None
            return int(iterations), base64.b64decode(salt, validate=True), base64.b64decode(digest, validate=True)
        except (ValueError, TypeError):
            return None
