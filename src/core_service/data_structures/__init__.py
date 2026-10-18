"""Data structures written from scratch (no reliance on ``collections``).

* :class:`Stack` (LIFO)
* :class:`DoublyLinkedList`
* :class:`Array` (dynamic array with merge sort and binary search)
* :class:`CircularArray` (ring buffer)
* :class:`BinaryHeap` (priority queue on an :class:`Array`) and :func:`smallest_k` (bounded top-k)
"""

from core_service.data_structures.array import Array
from core_service.data_structures.binary_heap import BinaryHeap, smallest_k
from core_service.data_structures.circular_array import CircularArray
from core_service.data_structures.doubly_linked_list import DoublyLinkedList
from core_service.data_structures.exceptions import EmptyStructureError
from core_service.data_structures.node import Node
from core_service.data_structures.stack import Stack

__all__ = [
    "Array",
    "BinaryHeap",
    "CircularArray",
    "DoublyLinkedList",
    "EmptyStructureError",
    "Node",
    "Stack",
    "smallest_k",
]
