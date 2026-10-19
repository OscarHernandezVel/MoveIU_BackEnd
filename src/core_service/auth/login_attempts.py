"""Brute-force protection for the login (story 1.2): 3 consecutive failures lock the account for 5 minutes.

Each e-mail owns a small LIFO stack of failure timestamps. The lock is decided by looking at
the top ``max_attempts`` entries only (``peek_many``): if they all happened within the lock
window of the most recent one, the account is locked until ``newest + lock_seconds``. A
successful login drains the stack. Unknown e-mails are tracked too, so the lock behaviour does
not reveal which accounts exist.

With several service replicas the counter must be shared, otherwise an attacker gets
``max_attempts`` tries *per replica*. When a :class:`DocumentStore` is given, the failures live
there (keyed by a hash of the e-mail, so no address is written to Redis) and the read-modify-write
is serialised with the injected lock manager. Without a store the state is in this process.
"""

from __future__ import annotations

import hashlib
import threading

from core_service.clock import Clock
from core_service.data_structures import Stack
from core_service.persistence.documents import DocumentStore
from core_service.services.keyed_locks import KeyedLocks, LockManager

MAX_TRACKED_KEYS = 10_000
COLLECTION = "login_failures"


class LoginAttemptTracker:
    def __init__(
        self,
        clock: Clock,
        max_attempts: int = 3,
        lock_seconds: float = 300.0,
        store: DocumentStore | None = None,
        locks: LockManager | None = None,
    ) -> None:
        self._clock = clock
        self._max_attempts = max_attempts
        self._lock_seconds = lock_seconds
        self._store = store
        self._locks: LockManager = locks or KeyedLocks()
        self._failures: dict[str, Stack[float]] = {}
        self._guard = threading.Lock()

    @staticmethod
    def _key(email: str) -> str:
        return email.strip().lower()

    @staticmethod
    def _shared_key(key: str) -> str:
        return hashlib.sha256(key.encode("utf-8")).hexdigest()[:32]

    # ------------------------------------------------------------------ storage
    def _load(self, key: str) -> Stack[float] | None:
        if self._store is None:
            with self._guard:
                return self._failures.get(key)
        document = self._store.get(COLLECTION, self._shared_key(key))
        if document is None:
            return None
        stack: Stack[float] = Stack(max_size=self._max_attempts)
        for stamp in reversed(document["failures"]):  # stored newest first: push the oldest first
            stack.push(float(stamp))
        return stack

    def _save(self, key: str, stack: Stack[float]) -> None:
        if self._store is None:
            with self._guard:
                if key not in self._failures and len(self._failures) >= MAX_TRACKED_KEYS:
                    del self._failures[next(iter(self._failures))]  # drop the oldest tracked e-mail
                self._failures[key] = stack
            return
        self._store.put(
            COLLECTION, self._shared_key(key), {"failures": list(stack)}, ttl_seconds=self._lock_seconds * 2
        )

    def _forget(self, key: str) -> Stack[float] | None:
        if self._store is None:
            with self._guard:
                return self._failures.pop(key, None)
        stack = self._load(key)
        self._store.delete(COLLECTION, self._shared_key(key))
        return stack

    # ---------------------------------------------------------------------- API
    def lock_remaining(self, email: str) -> float | None:
        """Seconds until the account unlocks, or ``None`` when it is not locked."""
        stack = self._load(self._key(email))
        if stack is None:
            return None
        recent = stack.peek_many(self._max_attempts)  # newest first
        if len(recent) < self._max_attempts:
            return None
        newest = recent[0]
        if newest - recent[-1] > self._lock_seconds:
            return None  # the failures are spread too far apart: not consecutive attempts
        remaining = newest + self._lock_seconds - self._clock.now()
        return remaining if remaining > 0 else None

    def record_failure(self, email: str) -> None:
        key = self._key(email)
        with self._locks.acquire(f"login:{self._shared_key(key)}"):
            stack = self._load(key) or Stack(max_size=self._max_attempts)
            stack.push(self._clock.now())
            self._save(key, stack)

    def record_success(self, email: str) -> None:
        key = self._key(email)
        with self._locks.acquire(f"login:{self._shared_key(key)}"):
            stack = self._forget(key)
            if stack is not None:
                stack.drain()
