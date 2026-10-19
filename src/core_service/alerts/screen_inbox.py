"""Per-device inbox of on-screen alerts, newest first.

Each device owns a bounded LIFO stack: the latest alert is on top, so ``latest()`` is a short
walk from the top, and old alerts fall off the bottom in O(1). The stack is rebuilt from the
document store on every operation (no per-process cache), so any replica sees the alerts pushed
by any other; the read-modify-write of a device is serialised with the injected lock manager.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import ContextManager

from core_service.alerts.screen_alert import ScreenAlert
from core_service.data_structures import Stack
from core_service.persistence.documents import DocumentStore
from core_service.services.keyed_locks import KeyedLocks, LockManager

COLLECTION = "screen_inbox"
RETENTION_SECONDS = 30 * 24 * 3600


@dataclass
class InboxItem:
    alert: ScreenAlert
    read: bool = False


class ScreenInbox:
    def __init__(
        self,
        store: DocumentStore,
        max_size: int = 50,
        retention_seconds: float = RETENTION_SECONDS,
        locks: LockManager | None = None,
    ) -> None:
        self._store = store
        self._max_size = max_size
        self._retention = retention_seconds
        self._locks: LockManager = locks or KeyedLocks()

    # ---------------------------------------------------------------- internals
    def _lock(self, device_id: str) -> ContextManager[None]:
        return self._locks.acquire(f"inbox:{device_id}")

    def _load(self, device_id: str) -> Stack[InboxItem]:
        stack: Stack[InboxItem] = Stack(max_size=self._max_size)
        document = self._store.get(COLLECTION, device_id)
        if document is not None:
            for raw in reversed(document.get("items", [])):  # stored newest first: push oldest first
                stack.push(InboxItem(ScreenAlert.from_dict(raw["alert"]), bool(raw["read"])))
        return stack

    def _save(self, device_id: str, stack: Stack[InboxItem]) -> None:
        items = [{"alert": item.alert.to_dict(), "read": item.read} for item in stack]
        self._store.put(COLLECTION, device_id, {"items": items}, ttl_seconds=self._retention)

    # ---------------------------------------------------------------------- API
    def push(self, device_id: str, alert: ScreenAlert) -> None:
        with self._lock(device_id):
            stack = self._load(device_id)
            stack.push(InboxItem(alert))
            self._save(device_id, stack)

    def latest(self, device_id: str, limit: int = 20) -> list[InboxItem]:
        return self._load(device_id).peek_many(limit)

    def unread_count(self, device_id: str) -> int:
        return sum(1 for item in self._load(device_id) if not item.read)

    def mark_read(self, device_id: str, notification_id: str) -> bool:
        with self._lock(device_id):
            stack = self._load(device_id)
            for item in stack:
                if item.alert.notification_id == notification_id:
                    item.read = True
                    self._save(device_id, stack)
                    return True
            return False

    def mark_all_read(self, device_id: str) -> int:
        with self._lock(device_id):
            stack = self._load(device_id)
            changed = 0
            for item in stack:
                if not item.read:
                    item.read = True
                    changed += 1
            if changed:
                self._save(device_id, stack)
            return changed

    def find(self, device_id: str, notification_id: str) -> InboxItem | None:
        for item in self._load(device_id):
            if item.alert.notification_id == notification_id:
                return item
        return None
