"""Synchronous, in-process publish/subscribe bus.

Producers do not know their consumers: the occupancy service publishes an event and the
alert, findings and (optional) Redis-publisher services react to it. A failing handler
never breaks the producer or the other handlers: the error is logged, counted and returned.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from typing import Any

from core_service.metrics import MetricsRegistry

log = logging.getLogger(__name__)

Handler = Callable[[Any], None]


class EventBus:
    def __init__(self, metrics: MetricsRegistry | None = None) -> None:
        self._subscribers: list[tuple[type, Handler]] = []
        self._lock = threading.Lock()
        self._metrics = metrics

    def subscribe(self, event_type: type, handler: Handler) -> None:
        with self._lock:
            self._subscribers.append((event_type, handler))

    def publish(self, event: object) -> list[Exception]:
        """Deliver ``event`` to every matching handler; returns the errors raised by handlers."""
        with self._lock:
            handlers = [handler for event_type, handler in self._subscribers if isinstance(event, event_type)]
        errors: list[Exception] = []
        for handler in handlers:
            try:
                handler(event)
            except Exception as error:  # noqa: BLE001 - isolation is the whole point
                errors.append(error)
                log.exception(
                    "Event handler %s failed for %s", getattr(handler, "__qualname__", handler), type(event).__name__
                )
                if self._metrics is not None:
                    self._metrics.increment("event_handler_errors", event=type(event).__name__)
        return errors
