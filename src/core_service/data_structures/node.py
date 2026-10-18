"""Node of a doubly linked list."""

from __future__ import annotations

from typing import Generic, TypeVar

T = TypeVar("T")


class Node(Generic[T]):
    """A list node with links to its previous and next neighbours.

    ``owner`` points to the list that currently holds the node. It lets the list
    reject foreign or already removed nodes in O(1) instead of corrupting itself.
    """

    __slots__ = ("value", "prev", "next", "owner")

    def __init__(self, value: T) -> None:
        self.value: T = value
        self.prev: Node[T] | None = None
        self.next: Node[T] | None = None
        self.owner: object | None = None

    def __repr__(self) -> str:
        return f"Node({self.value!r})"
