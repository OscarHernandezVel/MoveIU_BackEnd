"""LIFO compensation stack ("saga") for multi-step operations.

Each step that changes state registers how to undo itself. If a later step fails, the
registered compensations run in *reverse* order (last done, first undone), which is exactly
what a stack gives. This keeps in-memory structures, documents and SQL rows consistent
without a distributed transaction.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from types import TracebackType
from typing import TypeVar

from core_service.data_structures import Stack

log = logging.getLogger(__name__)

T = TypeVar("T")


class Saga:
    def __init__(self) -> None:
        self._compensations: Stack[Callable[[], object]] = Stack()

    def __len__(self) -> int:
        return len(self._compensations)

    def step(self, action: Callable[[], T], compensate: Callable[[], object] | None = None) -> T:
        """Run ``action``; when it succeeds, remember ``compensate`` for a later rollback."""
        result = action()
        if compensate is not None:
            self._compensations.push(compensate)
        return result

    def on_rollback(self, compensate: Callable[[], object]) -> None:
        """Register a compensation for a change that was already made."""
        self._compensations.push(compensate)

    def rollback(self) -> int:
        """Run every compensation, most recent first. A failing compensation is logged and the
        rest still run. Returns the number of compensations executed."""
        executed = 0
        while not self._compensations.is_empty():
            compensation = self._compensations.pop()
            try:
                compensation()
            except Exception:  # noqa: BLE001
                log.exception("Compensation failed during rollback")
            executed += 1
        return executed

    def __enter__(self) -> Saga:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        if exc_type is not None:
            self.rollback()
        else:
            self._compensations.clear()
