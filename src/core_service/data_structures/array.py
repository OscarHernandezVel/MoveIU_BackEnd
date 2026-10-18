"""Dynamic array over a manually managed block of memory.

Complexity summary (n = number of elements):

* index read / write: O(1)
* append: amortised O(1) (the block doubles when it is full)
* insert / remove_at: O(n) (elements are shifted)
* merge_sorted: O(n log n), stable
* bisect / binary_search: O(log n) on a sorted array
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Iterator
from typing import Any, Generic, TypeVar, cast

T = TypeVar("T")
KeyFunction = Callable[[T], Any]


class Array(Generic[T]):
    """Growable array with explicit capacity management."""

    _MIN_CAPACITY = 4

    def __init__(self, capacity: int = 8) -> None:
        if capacity < 1:
            raise ValueError("capacity must be positive")
        self._data: list[T | None] = [None] * capacity
        self._size = 0

    # ------------------------------------------------------------ constructors
    @classmethod
    def from_iterable(cls, items: Iterable[T]) -> Array[T]:
        array: Array[T] = cls()
        for item in items:
            array.append(item)
        return array

    @classmethod
    def filled(cls, size: int, factory: Callable[[], T]) -> Array[T]:
        """Create an array of ``size`` items built by ``factory`` (no shared references)."""
        if size < 0:
            raise ValueError("size must not be negative")
        array: Array[T] = cls(max(size, 1))
        for _ in range(size):
            array.append(factory())
        return array

    # ------------------------------------------------------------------- basics
    def __len__(self) -> int:
        return self._size

    @property
    def capacity(self) -> int:
        return len(self._data)

    def __iter__(self) -> Iterator[T]:
        for index in range(self._size):
            yield cast(T, self._data[index])

    def to_list(self) -> list[T]:
        return list(self)

    def _normalize(self, index: int, allow_end: bool = False) -> int:
        if not isinstance(index, int):
            raise TypeError("array indices must be integers")
        if index < 0:
            index += self._size
        limit = self._size if allow_end else self._size - 1
        if index < 0 or index > limit:
            raise IndexError("array index out of range")
        return index

    def __getitem__(self, index: int) -> T:
        return cast(T, self._data[self._normalize(index)])

    def __setitem__(self, index: int, value: T) -> None:
        self._data[self._normalize(index)] = value

    # ---------------------------------------------------------------- mutation
    def _grow(self, minimum: int) -> None:
        new_capacity = max(minimum, len(self._data) * 2)
        block: list[T | None] = [None] * new_capacity
        for i in range(self._size):
            block[i] = self._data[i]
        self._data = block

    def _maybe_shrink(self) -> None:
        capacity = len(self._data)
        if capacity > self._MIN_CAPACITY * 4 and self._size <= capacity // 4:
            new_capacity = capacity // 2
            block: list[T | None] = [None] * new_capacity
            for i in range(self._size):
                block[i] = self._data[i]
            self._data = block

    def append(self, value: T) -> None:
        if self._size == len(self._data):
            self._grow(self._size + 1)
        self._data[self._size] = value
        self._size += 1

    def extend(self, items: Iterable[T]) -> None:
        for item in items:
            self.append(item)

    def insert(self, index: int, value: T) -> None:
        position = self._normalize(index, allow_end=True)
        if self._size == len(self._data):
            self._grow(self._size + 1)
        for i in range(self._size, position, -1):
            self._data[i] = self._data[i - 1]
        self._data[position] = value
        self._size += 1

    def remove_at(self, index: int = -1) -> T:
        position = self._normalize(index)
        value = cast(T, self._data[position])
        for i in range(position, self._size - 1):
            self._data[i] = self._data[i + 1]
        self._data[self._size - 1] = None
        self._size -= 1
        self._maybe_shrink()
        return value

    def clear(self) -> None:
        self._data = [None] * self._MIN_CAPACITY
        self._size = 0

    # ----------------------------------------------------------------- search
    def index_where(self, predicate: Callable[[T], bool]) -> int | None:
        """Linear search: index of the first item matching ``predicate``."""
        for i in range(self._size):
            if predicate(cast(T, self._data[i])):
                return i
        return None

    def count_where(self, predicate: Callable[[T], bool]) -> int:
        return sum(1 for item in self if predicate(item))

    def bisect_left(self, target: Any, key: KeyFunction[T] | None = None) -> int:
        """Leftmost index whose key is >= ``target`` (array must be sorted ascending)."""
        low, high = 0, self._size
        while low < high:
            mid = (low + high) // 2
            item = cast(T, self._data[mid])
            mid_key = key(item) if key is not None else item
            if mid_key < target:
                low = mid + 1
            else:
                high = mid
        return low

    def bisect_right(self, target: Any, key: KeyFunction[T] | None = None) -> int:
        """Leftmost index whose key is > ``target`` (array must be sorted ascending)."""
        low, high = 0, self._size
        while low < high:
            mid = (low + high) // 2
            item = cast(T, self._data[mid])
            mid_key = key(item) if key is not None else item
            if target < mid_key:
                high = mid
            else:
                low = mid + 1
        return low

    def binary_search(self, target: Any, key: KeyFunction[T] | None = None) -> int | None:
        index = self.bisect_left(target, key)
        if index < self._size:
            item = cast(T, self._data[index])
            found = key(item) if key is not None else item
            if found == target:
                return index
        return None

    # ---------------------------------------------------------------- sorting
    def merge_sorted(self, key: KeyFunction[T] | None = None, reverse: bool = False) -> Array[T]:
        """Return a new, stably sorted array (top-down merge sort)."""
        items = self.to_list()
        sorted_items = _merge_sort(items, key, reverse)
        return Array.from_iterable(sorted_items)


def _merge_sort(items: list[T], key: KeyFunction[T] | None, reverse: bool) -> list[T]:
    if len(items) <= 1:
        return list(items)
    middle = len(items) // 2
    left = _merge_sort(items[:middle], key, reverse)
    right = _merge_sort(items[middle:], key, reverse)
    merged: list[T] = []
    i = j = 0
    while i < len(left) and j < len(right):
        left_key: Any = key(left[i]) if key is not None else left[i]
        right_key: Any = key(right[j]) if key is not None else right[j]
        # ``<=`` (or ``>=`` when descending) takes from the left on ties: stable.
        take_left = left_key >= right_key if reverse else left_key <= right_key
        if take_left:
            merged.append(left[i])
            i += 1
        else:
            merged.append(right[j])
            j += 1
    merged.extend(left[i:])
    merged.extend(right[j:])
    return merged
