"""Doubly linked list.

Complexity summary (n = number of elements):

* append / prepend / insert_before / insert_after / remove(node): O(1)
* move_to_front / move_to_back (re-link a node without allocating): O(1)
* pop_front / pop_back: O(1)
* iteration in either direction: O(n)
* find_node: O(n)
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Iterator
from typing import Generic, TypeVar

from core_service.data_structures.exceptions import EmptyStructureError
from core_service.data_structures.node import Node

T = TypeVar("T")


class DoublyLinkedList(Generic[T]):
    """Sequence with O(1) insertion and removal at any known node."""

    def __init__(self, items: Iterable[T] | None = None) -> None:
        self._head: Node[T] | None = None
        self._tail: Node[T] | None = None
        self._size = 0
        if items is not None:
            for item in items:
                self.append(item)

    # ------------------------------------------------------------------ views
    def __len__(self) -> int:
        return self._size

    def __iter__(self) -> Iterator[T]:
        node = self._head
        while node is not None:
            yield node.value
            node = node.next

    def __reversed__(self) -> Iterator[T]:
        node = self._tail
        while node is not None:
            yield node.value
            node = node.prev

    @property
    def head(self) -> Node[T] | None:
        return self._head

    @property
    def tail(self) -> Node[T] | None:
        return self._tail

    def to_list(self) -> list[T]:
        return list(self)

    def nodes(self, start: Node[T] | None = None, reverse: bool = False) -> Iterator[Node[T]]:
        """Yield nodes from ``start`` (or an end of the list) in the chosen direction.

        The next node is read before yielding, so the *yielded* node may be removed by the
        caller while iterating. Removing any *other* node ahead of the cursor is not supported:
        a stale ``following`` handle would end the walk early, so we detect it and fail loudly.
        """
        if start is not None:
            self._check_owner(start)
            node: Node[T] | None = start
        else:
            node = self._tail if reverse else self._head
        while node is not None:
            following = node.prev if reverse else node.next
            yield node
            if following is not None and following.owner is not self:
                raise RuntimeError("the list was modified while iterating over its nodes")
            node = following

    def find_node(self, predicate: Callable[[T], bool]) -> Node[T] | None:
        for node in self.nodes():
            if predicate(node.value):
                return node
        return None

    # -------------------------------------------------------------- mutation
    def _check_owner(self, node: Node[T]) -> None:
        if node.owner is not self:
            raise ValueError("node does not belong to this list")

    def append(self, value: T) -> Node[T]:
        node = Node(value)
        node.owner = self
        if self._tail is None:
            self._head = self._tail = node
        else:
            node.prev = self._tail
            self._tail.next = node
            self._tail = node
        self._size += 1
        return node

    def prepend(self, value: T) -> Node[T]:
        node = Node(value)
        node.owner = self
        if self._head is None:
            self._head = self._tail = node
        else:
            node.next = self._head
            self._head.prev = node
            self._head = node
        self._size += 1
        return node

    def insert_after(self, anchor: Node[T], value: T) -> Node[T]:
        self._check_owner(anchor)
        node = Node(value)
        node.owner = self
        node.prev = anchor
        node.next = anchor.next
        if anchor.next is not None:
            anchor.next.prev = node
        else:
            self._tail = node
        anchor.next = node
        self._size += 1
        return node

    def insert_before(self, anchor: Node[T], value: T) -> Node[T]:
        self._check_owner(anchor)
        node = Node(value)
        node.owner = self
        node.next = anchor
        node.prev = anchor.prev
        if anchor.prev is not None:
            anchor.prev.next = node
        else:
            self._head = node
        anchor.prev = node
        self._size += 1
        return node

    def remove(self, node: Node[T]) -> T:
        """Unlink ``node`` in O(1) and return its value."""
        self._check_owner(node)
        if node.prev is not None:
            node.prev.next = node.next
        else:
            self._head = node.next
        if node.next is not None:
            node.next.prev = node.prev
        else:
            self._tail = node.prev
        node.prev = node.next = None
        node.owner = None
        self._size -= 1
        return node.value

    def contains(self, node: Node[T]) -> bool:
        """Whether ``node`` is currently linked in *this* list (O(1); stale handles answer False)."""
        return node.owner is self

    def move_to_front(self, node: Node[T]) -> None:
        """Re-link ``node`` as the head in O(1). The node object (and every handle to it) stays valid."""
        self._check_owner(node)
        head = self._head
        if node is head or head is None:
            return
        # ``node`` is not the head, so it has a predecessor.
        assert node.prev is not None  # noqa: S101 - structural invariant
        node.prev.next = node.next
        if node.next is not None:
            node.next.prev = node.prev
        else:
            self._tail = node.prev
        node.prev = None
        node.next = head
        head.prev = node
        self._head = node

    def move_to_back(self, node: Node[T]) -> None:
        """Re-link ``node`` as the tail in O(1)."""
        self._check_owner(node)
        tail = self._tail
        if node is tail or tail is None:
            return
        assert node.next is not None  # noqa: S101 - structural invariant
        node.next.prev = node.prev
        if node.prev is not None:
            node.prev.next = node.next
        else:
            self._head = node.next
        node.next = None
        node.prev = tail
        tail.next = node
        self._tail = node

    def pop_front(self) -> T:
        if self._head is None:
            raise EmptyStructureError("pop from empty list")
        return self.remove(self._head)

    def pop_back(self) -> T:
        if self._tail is None:
            raise EmptyStructureError("pop from empty list")
        return self.remove(self._tail)

    def clear(self) -> None:
        node = self._head
        while node is not None:
            following = node.next
            node.prev = node.next = None
            node.owner = None
            node = following
        self._head = self._tail = None
        self._size = 0
