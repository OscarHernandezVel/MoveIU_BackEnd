"""Fixed-capacity circular buffer (ring buffer)."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Generic, TypeVar, cast

from core_service.data_structures.exceptions import EmptyStructureError

T = TypeVar("T")


class CircularArray(Generic[T]):
    """Keeps the most recent ``capacity`` items; ``push`` is O(1) and never reallocates."""

    def __init__(self, capacity: int) -> None:
        if capacity < 1:
            raise ValueError("capacity must be positive")
        self._data: list[T | None] = [None] * capacity
        self._start = 0
        self._size = 0

    @property
    def capacity(self) -> int:
        return len(self._data)

    def __len__(self) -> int:
        return self._size

    def is_full(self) -> bool:
        return self._size == len(self._data)

    def push(self, item: T) -> T | None:
        """Add ``item``; returns the overwritten (oldest) item when the buffer was full."""
        capacity = len(self._data)
        if self._size < capacity:
            self._data[(self._start + self._size) % capacity] = item
            self._size += 1
            return None
        evicted = self._data[self._start]
        self._data[self._start] = item
        self._start = (self._start + 1) % capacity
        return evicted

    def __iter__(self) -> Iterator[T]:
        """Iterate from the oldest to the newest item."""
        capacity = len(self._data)
        for offset in range(self._size):
            yield cast(T, self._data[(self._start + offset) % capacity])

    def newest_first(self) -> Iterator[T]:
        capacity = len(self._data)
        for offset in range(self._size - 1, -1, -1):
            yield cast(T, self._data[(self._start + offset) % capacity])

    def to_list(self) -> list[T]:
        return list(self)

    def newest(self) -> T:
        if self._size == 0:
            raise EmptyStructureError("buffer is empty")
        return cast(T, self._data[(self._start + self._size - 1) % len(self._data)])

    def oldest(self) -> T:
        if self._size == 0:
            raise EmptyStructureError("buffer is empty")
        return cast(T, self._data[self._start])

    def last(self, count: int) -> list[T]:
        """Up to ``count`` items, newest first."""
        if count < 0:
            raise ValueError("count must not be negative")
        result: list[T] = []
        for item in self.newest_first():
            if len(result) >= count:
                break
            result.append(item)
        return result

    def clear(self) -> None:
        self._data = [None] * len(self._data)
        self._start = 0
        self._size = 0
