"""In-memory index of active alerts, grouped by stop.

Each stop keeps its alerts in a doubly linked list, and a dictionary keeps the node of
every alert, so removing an alert (delivered, cancelled, expired) is O(1).
"""

from __future__ import annotations

import threading
from collections.abc import Iterable

from core_service.data_structures import DoublyLinkedList, Node
from core_service.models import ProximityAlert


class AlertIndex:
    def __init__(self) -> None:
        self._by_stop: dict[str, DoublyLinkedList[ProximityAlert]] = {}
        self._nodes: dict[str, Node[ProximityAlert]] = {}
        self._lock = threading.RLock()

    def __len__(self) -> int:
        with self._lock:
            return len(self._nodes)

    def add(self, alert: ProximityAlert) -> None:
        with self._lock:
            if alert.alert_id in self._nodes:
                self.remove(alert.alert_id)
            alerts = self._by_stop.setdefault(alert.stop_id, DoublyLinkedList())
            self._nodes[alert.alert_id] = alerts.append(alert)

    def remove(self, alert_id: str) -> ProximityAlert | None:
        with self._lock:
            node = self._nodes.pop(alert_id, None)
            if node is None:
                return None
            alert = node.value
            alerts = self._by_stop[alert.stop_id]
            alerts.remove(node)
            if len(alerts) == 0:
                del self._by_stop[alert.stop_id]
            return alert

    def get(self, alert_id: str) -> ProximityAlert | None:
        with self._lock:
            node = self._nodes.get(alert_id)
            return None if node is None else node.value

    def for_stop(self, stop_id: str) -> list[ProximityAlert]:
        """Snapshot of the alerts waiting at ``stop_id``."""
        with self._lock:
            alerts = self._by_stop.get(stop_id)
            return [] if alerts is None else alerts.to_list()

    def all(self) -> list[ProximityAlert]:
        with self._lock:
            return [node.value for node in self._nodes.values()]

    def replace_all(self, alerts: Iterable[ProximityAlert]) -> None:
        """Swap the whole content in one step (reconciliation with the database).

        The new lists are built *aside* and installed under the lock, so a bus update that runs
        during the reload never sees an empty index (``clear`` + ``add`` would open that gap)."""
        by_stop: dict[str, DoublyLinkedList[ProximityAlert]] = {}
        nodes: dict[str, Node[ProximityAlert]] = {}
        for alert in alerts:
            if alert.alert_id in nodes:  # duplicated input: the last one wins, no orphan node
                previous = nodes.pop(alert.alert_id)
                by_stop[previous.value.stop_id].remove(previous)
            nodes[alert.alert_id] = by_stop.setdefault(alert.stop_id, DoublyLinkedList()).append(alert)
        with self._lock:
            self._by_stop = by_stop
            self._nodes = nodes

    def clear(self) -> None:
        with self._lock:
            self._by_stop.clear()
            self._nodes.clear()
