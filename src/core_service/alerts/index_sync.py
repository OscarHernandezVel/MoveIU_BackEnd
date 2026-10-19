"""Keeps the in-memory alert index of every service replica in step.

Each replica holds the alerts waiting at each stop in a doubly linked list (O(1) removal). An
alert created, cancelled or delivered on one replica must reach the others, otherwise a bus whose
readings land on replica B would never trigger an alert that was created on replica A.

* **Push (fast)**: the replica that changes an alert publishes the change on a Redis channel; the
  others apply it to their local index within milliseconds.
* **Pull (safe)**: pub/sub is fire-and-forget, so a periodic reconciliation job reloads the active
  alerts from PostgreSQL (the source of truth) and swaps them into the index. A missed message can
  only delay an alert, never lose it, and the compare-and-set in SQL still guarantees that an alert
  is delivered *once* even if two replicas briefly disagree.
"""

from __future__ import annotations

import json
import logging
import threading
import uuid
from collections.abc import Callable
from typing import Any, Protocol

import redis
from redis.exceptions import RedisError

from core_service.alerts.alert_index import AlertIndex
from core_service.metrics import MetricsRegistry
from core_service.models import ProximityAlert

log = logging.getLogger(__name__)


class IndexSync(Protocol):
    def announce_added(self, alert: ProximityAlert) -> None: ...

    def announce_removed(self, alert_id: str) -> None: ...


class NullIndexSync:
    """Single replica: nothing to tell anybody."""

    def announce_added(self, alert: ProximityAlert) -> None:
        return None

    def announce_removed(self, alert_id: str) -> None:
        return None


class RedisIndexReplicator:
    def __init__(
        self,
        client: redis.Redis,
        index: AlertIndex,
        prefix: str = "core",
        metrics: MetricsRegistry | None = None,
        on_resync: Callable[[], object] | None = None,
    ) -> None:
        self._redis = client
        self._index = index
        self._channel = f"{prefix}:alert-index"
        self._node_id = uuid.uuid4().hex
        self._metrics = metrics
        self._on_resync = on_resync
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def set_resync(self, callback: Callable[[], object]) -> None:
        """Function that reloads the index from the database (called after every (re)subscription)."""
        self._on_resync = callback

    # --------------------------------------------------------------- publishing
    def announce_added(self, alert: ProximityAlert) -> None:
        self._publish({"op": "add", "alert": alert.to_dict()})

    def announce_removed(self, alert_id: str) -> None:
        self._publish({"op": "remove", "alert_id": alert_id})

    def _publish(self, message: dict[str, Any]) -> None:
        message["origin"] = self._node_id
        try:
            self._redis.publish(self._channel, json.dumps(message, separators=(",", ":")))
        except RedisError:
            # Never fail the user's request because a broadcast failed: reconciliation will fix it.
            log.warning("Could not broadcast an alert index change")
            if self._metrics is not None:
                self._metrics.increment("alert_index_publish_failed")

    # ---------------------------------------------------------------- receiving
    def handle(self, raw: str) -> bool:
        """Apply one message from another replica. Returns whether the index changed."""
        try:
            message = json.loads(raw)
            if message.get("origin") == self._node_id:
                return False  # our own change, already applied locally
            if message["op"] == "add":
                self._index.add(ProximityAlert.from_dict(message["alert"]))
                return True
            if message["op"] == "remove":
                return self._index.remove(message["alert_id"]) is not None
        except (ValueError, KeyError, TypeError, AttributeError):
            log.warning("Ignoring a malformed alert index message")
        return False

    def start(self) -> None:
        if self._thread is not None:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="alert-index-sync", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                pubsub = self._redis.pubsub(ignore_subscribe_messages=True)  # type: ignore[no-untyped-call]
                pubsub.subscribe(self._channel)
                if self._on_resync is not None:
                    self._on_resync()  # we may have missed messages while disconnected
                while not self._stop.is_set():
                    message = pubsub.get_message(timeout=1.0)
                    if message is not None and message.get("type") == "message":
                        self.handle(str(message["data"]))
                pubsub.close()
            except RedisError:
                log.warning("Alert index subscription lost; retrying")
                if self._stop.wait(1.0):
                    return
