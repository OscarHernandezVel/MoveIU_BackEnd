"""Binary heap on top of :class:`Array`, and a bounded top-k selection.

The heap is a complete binary tree stored level by level in the array: the children of index ``i`` are
``2i + 1`` and ``2i + 2``. ``push`` and ``pop`` sift one path of the tree: O(log n).

:func:`smallest_k` keeps a heap of at most ``k`` elements whose *root is the worst* of the kept ones;
each new element either replaces the root or is discarded. Selecting the ``k`` nearest buses among ``n``
costs O(n log k) instead of O(n log n) for sorting everything (and O(k) memory).

Ties are broken by insertion order, so results are deterministic and :func:`smallest_k` is stable.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from typing import Any, Generic, TypeVar

from core_service.data_structures.array import Array
from core_service.data_structures.exceptions import EmptyStructureError

T = TypeVar("T")


class BinaryHeap(Generic[T]):
    """Min-heap by ``key`` (or max-heap with ``largest_first=True``)."""

    def __init__(self, key: Callable[[T], Any] | None = None, *, largest_first: bool = False) -> None:
        self._key: Callable[[T], Any] = key or (lambda item: item)
        self._largest_first = largest_first
        self._items: Array[tuple[Any, int, T]] = Array()
        self._counter = 0

    def __len__(self) -> int:
        return len(self._items)

    def is_empty(self) -> bool:
        return len(self._items) == 0

    def _before(self, a: tuple[Any, int, T], b: tuple[Any, int, T]) -> bool:
        """Whether ``a`` belongs above ``b``. Ties: a min-heap pops in insertion order, and a max-heap pops in
        exactly the reverse of that order (so reversing its output is a stable ascending order)."""
        if a[0] == b[0]:
            return a[1] > b[1] if self._largest_first else a[1] < b[1]
        return bool(a[0] > b[0]) if self._largest_first else bool(a[0] < b[0])

    def push(self, item: T) -> None:
        self._items.append((self._key(item), self._counter, item))
        self._counter += 1
        self._sift_up(len(self._items) - 1)

    def peek(self) -> T:
        if self.is_empty():
            raise EmptyStructureError("peek from an empty heap")
        return self._items[0][2]

    def pop(self) -> T:
        if self.is_empty():
            raise EmptyStructureError("pop from an empty heap")
        top = self._items[0]
        last = self._items.remove_at(-1)
        if not self.is_empty():
            self._items[0] = last
            self._sift_down(0)
        return top[2]

    def replace_top(self, item: T) -> T:
        """Pop the root and push ``item`` in one sift (O(log n))."""
        if self.is_empty():
            raise EmptyStructureError("replace on an empty heap")
        top = self._items[0]
        self._items[0] = (self._key(item), self._counter, item)
        self._counter += 1
        self._sift_down(0)
        return top[2]

    def drain(self) -> list[T]:
        """Remove every element, in heap order."""
        result: list[T] = []
        while not self.is_empty():
            result.append(self.pop())
        return result

    # ------------------------------------------------------------------ sifting
    def _sift_up(self, index: int) -> None:
        items = self._items
        while index > 0:
            parent = (index - 1) // 2
            if not self._before(items[index], items[parent]):
                break
            items[index], items[parent] = items[parent], items[index]
            index = parent

    def _sift_down(self, index: int) -> None:
        items = self._items
        size = len(items)
        while True:
            left, right, best = 2 * index + 1, 2 * index + 2, index
            if left < size and self._before(items[left], items[best]):
                best = left
            if right < size and self._before(items[right], items[best]):
                best = right
            if best == index:
                return
            items[index], items[best] = items[best], items[index]
            index = best


def smallest_k(items: Iterable[T], k: int, key: Callable[[T], Any] | None = None) -> list[T]:
    """The ``k`` smallest items by ``key``, in ascending order. O(n log k) time, O(k) memory."""
    if k <= 0:
        return []
    keyed: Callable[[T], Any] = key or (lambda item: item)
    # Max-heap of the kept items: its root is the worst kept one, the first to be displaced.
    kept: BinaryHeap[T] = BinaryHeap(keyed, largest_first=True)
    for item in items:
        if len(kept) < k:
            kept.push(item)
        elif keyed(item) < keyed(kept.peek()):
            kept.replace_top(item)
    return list(reversed(kept.drain()))
