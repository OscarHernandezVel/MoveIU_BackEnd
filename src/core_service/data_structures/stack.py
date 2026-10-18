"""LIFO stack, optionally bounded.

The stack is stored in a doubly linked list whose *head* is the top. That gives O(1)
``push``/``pop``/``peek`` and also O(1) eviction of the *oldest* element (the tail)
when the stack is bounded. A plain array-backed stack would need O(n) to drop the
bottom element.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Generic, TypeVar

from core_service.data_structures.doubly_linked_list import DoublyLinkedList
from core_service.data_structures.exceptions import EmptyStructureError

T = TypeVar("T")


class Stack(Generic[T]):
    """Last-in, first-out collection."""

    def __init__(self, max_size: int | None = None) -> None:
        if max_size is not None and max_size < 1:
            raise ValueError("max_size must be positive")
        self._items: DoublyLinkedList[T] = DoublyLinkedList()
        self._max_size = max_size

    @property
    def max_size(self) -> int | None:
        return self._max_size

    def __len__(self) -> int:
        return len(self._items)

    def __iter__(self) -> Iterator[T]:
        """Iterate from the top (most recent) to the bottom (oldest)."""
        return iter(self._items)

    def is_empty(self) -> bool:
        return len(self._items) == 0

    def push(self, item: T) -> T | None:
        """Push ``item``. When the stack is bounded and full, the oldest item is
        evicted and returned; otherwise ``None`` is returned."""
        self._items.prepend(item)
        if self._max_size is not None and len(self._items) > self._max_size:
            return self._items.pop_back()
        return None

    def pop(self) -> T:
        if self.is_empty():
            raise EmptyStructureError("pop from empty stack")
        return self._items.pop_front()

    def peek(self) -> T:
        head = self._items.head
        if head is None:
            raise EmptyStructureError("peek from empty stack")
        return head.value

    def peek_or_none(self) -> T | None:
        head = self._items.head
        return None if head is None else head.value

    def peek_many(self, count: int) -> list[T]:
        """Return up to ``count`` items, most recent first, without removing them. O(count)."""
        if count < 0:
            raise ValueError("count must not be negative")
        result: list[T] = []
        for item in self._items:
            if len(result) >= count:
                break
            result.append(item)
        return result

    def drain(self) -> list[T]:
        """Pop every item (most recent first) and return them."""
        drained: list[T] = []
        while not self.is_empty():
            drained.append(self.pop())
        return drained

    def clear(self) -> None:
        self._items.clear()
