"""Unit and differential tests of the hand-written data structures."""

from __future__ import annotations

import random

import pytest

from core_service.data_structures import (
    Array,
    CircularArray,
    DoublyLinkedList,
    EmptyStructureError,
    Stack,
)


# ----------------------------------------------------------------- Stack (LIFO)
class TestStack:
    def test_is_last_in_first_out(self) -> None:
        stack: Stack[int] = Stack()
        for value in (1, 2, 3):
            stack.push(value)
        assert [stack.pop(), stack.pop(), stack.pop()] == [3, 2, 1]

    def test_peek_does_not_remove(self) -> None:
        stack: Stack[str] = Stack()
        stack.push("a")
        stack.push("b")
        assert stack.peek() == "b"
        assert len(stack) == 2

    def test_empty_stack_raises(self) -> None:
        stack: Stack[int] = Stack()
        with pytest.raises(EmptyStructureError):
            stack.pop()
        with pytest.raises(EmptyStructureError):
            stack.peek()
        assert stack.peek_or_none() is None

    def test_peek_many_returns_most_recent_first(self) -> None:
        stack: Stack[int] = Stack()
        for value in range(5):
            stack.push(value)
        assert stack.peek_many(3) == [4, 3, 2]
        assert stack.peek_many(99) == [4, 3, 2, 1, 0]
        assert stack.peek_many(0) == []
        with pytest.raises(ValueError):
            stack.peek_many(-1)

    def test_bounded_stack_evicts_the_oldest(self) -> None:
        stack: Stack[int] = Stack(max_size=3)
        assert [stack.push(v) for v in (1, 2, 3)] == [None, None, None]
        assert stack.push(4) == 1  # oldest evicted
        assert list(stack) == [4, 3, 2]
        assert len(stack) == 3

    def test_drain_empties_in_lifo_order(self) -> None:
        stack: Stack[int] = Stack()
        for value in (1, 2, 3):
            stack.push(value)
        assert stack.drain() == [3, 2, 1]
        assert stack.is_empty()

    def test_invalid_max_size(self) -> None:
        with pytest.raises(ValueError):
            Stack(max_size=0)

    def test_differential_against_python_list(self) -> None:
        rng = random.Random(7)
        stack: Stack[int] = Stack()
        oracle: list[int] = []
        for step in range(2000):
            if oracle and rng.random() < 0.45:
                assert stack.pop() == oracle.pop()
            else:
                stack.push(step)
                oracle.append(step)
            assert len(stack) == len(oracle)
            if oracle:
                assert stack.peek() == oracle[-1]
        assert list(stack) == oracle[::-1]


# ------------------------------------------------------------- DoublyLinkedList
class TestDoublyLinkedList:
    def test_append_prepend_and_iteration_both_ways(self) -> None:
        items = DoublyLinkedList([2, 3])
        items.prepend(1)
        items.append(4)
        assert list(items) == [1, 2, 3, 4]
        assert list(reversed(items)) == [4, 3, 2, 1]
        assert len(items) == 4

    def test_insert_before_and_after(self) -> None:
        items: DoublyLinkedList[int] = DoublyLinkedList()
        middle = items.append(2)
        items.insert_before(middle, 1)
        items.insert_after(middle, 3)
        assert items.to_list() == [1, 2, 3]
        assert items.head is not None and items.head.value == 1
        assert items.tail is not None and items.tail.value == 3

    def test_remove_updates_neighbours_and_ends(self) -> None:
        items: DoublyLinkedList[int] = DoublyLinkedList()
        nodes = [items.append(v) for v in (1, 2, 3)]
        assert items.remove(nodes[1]) == 2
        assert items.to_list() == [1, 3]
        items.remove(nodes[0])
        items.remove(nodes[2])
        assert len(items) == 0 and items.head is None and items.tail is None

    def test_foreign_or_removed_nodes_are_rejected(self) -> None:
        first: DoublyLinkedList[int] = DoublyLinkedList()
        second: DoublyLinkedList[int] = DoublyLinkedList()
        node = first.append(1)
        with pytest.raises(ValueError):
            second.remove(node)
        first.remove(node)
        with pytest.raises(ValueError):
            first.remove(node)
        with pytest.raises(ValueError):
            first.insert_after(node, 2)

    def test_pop_front_and_back(self) -> None:
        items = DoublyLinkedList([1, 2, 3])
        assert items.pop_front() == 1
        assert items.pop_back() == 3
        assert items.to_list() == [2]
        items.pop_front()
        with pytest.raises(EmptyStructureError):
            items.pop_front()
        with pytest.raises(EmptyStructureError):
            items.pop_back()

    def test_nodes_walk_forward_and_backward_from_a_node(self) -> None:
        items: DoublyLinkedList[int] = DoublyLinkedList()
        nodes = [items.append(v) for v in range(5)]
        assert [n.value for n in items.nodes(start=nodes[2])] == [2, 3, 4]
        assert [n.value for n in items.nodes(start=nodes[2], reverse=True)] == [2, 1, 0]
        assert [n.value for n in items.nodes(reverse=True)] == [4, 3, 2, 1, 0]

    def test_nodes_can_be_removed_while_iterating(self) -> None:
        items = DoublyLinkedList(range(6))
        for node in items.nodes():
            if node.value % 2 == 0:
                items.remove(node)
        assert items.to_list() == [1, 3, 5]

    def test_find_node_and_clear(self) -> None:
        items = DoublyLinkedList(["a", "b", "c"])
        found = items.find_node(lambda v: v == "b")
        assert found is not None and found.value == "b"
        assert items.find_node(lambda v: v == "z") is None
        head = items.head
        items.clear()
        assert len(items) == 0
        assert head is not None and head.owner is None

    def test_differential_against_python_list(self) -> None:
        rng = random.Random(11)
        items: DoublyLinkedList[int] = DoublyLinkedList()
        oracle: list[int] = []
        for step in range(1500):
            action = rng.choice(["append", "prepend", "remove", "insert_after"])
            if action == "append":
                items.append(step)
                oracle.append(step)
            elif action == "prepend":
                items.prepend(step)
                oracle.insert(0, step)
            elif oracle:
                index = rng.randrange(len(oracle))
                node = list(items.nodes())[index]
                if action == "remove":
                    assert items.remove(node) == oracle.pop(index)
                else:
                    items.insert_after(node, step)
                    oracle.insert(index + 1, step)
            assert items.to_list() == oracle
            assert list(reversed(items)) == oracle[::-1]


# ------------------------------------------------------------------------ Array
class TestArray:
    def test_append_grows_capacity_by_doubling(self) -> None:
        array: Array[int] = Array(capacity=2)
        for value in range(9):
            array.append(value)
        assert len(array) == 9
        assert array.capacity >= 9
        assert array.to_list() == list(range(9))

    def test_index_access_and_bounds(self) -> None:
        array = Array.from_iterable([10, 20, 30])
        assert array[0] == 10 and array[-1] == 30
        array[1] = 25
        assert array[1] == 25
        with pytest.raises(IndexError):
            _ = array[3]
        with pytest.raises(IndexError):
            array[-4] = 0
        with pytest.raises(TypeError):
            _ = array["x"]  # type: ignore[index]

    def test_insert_and_remove_shift_elements(self) -> None:
        array = Array.from_iterable([1, 2, 4])
        array.insert(2, 3)
        array.insert(0, 0)
        array.insert(len(array), 5)
        assert array.to_list() == [0, 1, 2, 3, 4, 5]
        assert array.remove_at(0) == 0
        assert array.remove_at(-1) == 5
        assert array.remove_at(1) == 2
        assert array.to_list() == [1, 3, 4]
        with pytest.raises(IndexError):
            array.insert(10, 9)

    def test_shrinks_after_many_removals(self) -> None:
        array = Array.from_iterable(range(200))
        big_capacity = array.capacity
        while len(array) > 10:
            array.remove_at()
        assert array.capacity < big_capacity
        assert array.to_list() == list(range(10))

    def test_filled_uses_the_factory_for_each_cell(self) -> None:
        array = Array.filled(3, list)
        array[0].append(1)
        assert array[1] == [] and array[2] == []
        assert len(Array.filled(0, int)) == 0
        with pytest.raises(ValueError):
            Array.filled(-1, int)

    def test_merge_sorted_is_stable_and_supports_keys_and_reverse(self) -> None:
        pairs = Array.from_iterable([(2, "a"), (1, "b"), (2, "c"), (1, "d")])
        ascending = pairs.merge_sorted(key=lambda p: p[0])
        assert ascending.to_list() == [(1, "b"), (1, "d"), (2, "a"), (2, "c")]
        descending = pairs.merge_sorted(key=lambda p: p[0], reverse=True)
        assert descending.to_list() == [(2, "a"), (2, "c"), (1, "b"), (1, "d")]
        assert pairs.to_list() == [(2, "a"), (1, "b"), (2, "c"), (1, "d")]  # original untouched

    def test_merge_sort_matches_sorted_on_random_data(self) -> None:
        rng = random.Random(3)
        data = [rng.randint(-50, 50) for _ in range(500)]
        assert Array.from_iterable(data).merge_sorted().to_list() == sorted(data)
        assert Array.from_iterable(data).merge_sorted(reverse=True).to_list() == sorted(data, reverse=True)

    def test_bisect_and_binary_search(self) -> None:
        array = Array.from_iterable([1, 3, 3, 5, 9])
        assert array.bisect_left(3) == 1
        assert array.bisect_right(3) == 3
        assert array.bisect_left(0) == 0
        assert array.bisect_right(10) == 5
        assert array.binary_search(5) == 3
        assert array.binary_search(4) is None
        assert array.binary_search(10) is None

    def test_bisect_with_key(self) -> None:
        array = Array.from_iterable([("a", 0.1), ("b", 0.4), ("c", 0.9)])
        assert array.bisect_right(0.4, key=lambda item: item[1]) == 2
        assert array.binary_search(0.9, key=lambda item: item[1]) == 2

    def test_bisect_matches_the_standard_library(self) -> None:
        import bisect

        rng = random.Random(5)
        data = sorted(rng.randint(0, 40) for _ in range(300))
        array = Array.from_iterable(data)
        for target in range(-2, 45):
            assert array.bisect_left(target) == bisect.bisect_left(data, target)
            assert array.bisect_right(target) == bisect.bisect_right(data, target)

    def test_index_where_count_where_and_clear(self) -> None:
        array = Array.from_iterable([1, 2, 3, 4])
        assert array.index_where(lambda v: v > 2) == 2
        assert array.index_where(lambda v: v > 9) is None
        assert array.count_where(lambda v: v % 2 == 0) == 2
        array.clear()
        assert len(array) == 0

    def test_invalid_capacity(self) -> None:
        with pytest.raises(ValueError):
            Array(capacity=0)


# ---------------------------------------------------------------- CircularArray
class TestCircularArray:
    def test_overwrites_the_oldest_when_full(self) -> None:
        ring: CircularArray[int] = CircularArray(3)
        assert [ring.push(v) for v in (1, 2, 3)] == [None, None, None]
        assert ring.is_full()
        assert ring.push(4) == 1
        assert ring.push(5) == 2
        assert ring.to_list() == [3, 4, 5]
        assert ring.oldest() == 3 and ring.newest() == 5

    def test_last_returns_newest_first(self) -> None:
        ring: CircularArray[int] = CircularArray(4)
        for value in range(6):
            ring.push(value)
        assert ring.last(2) == [5, 4]
        assert ring.last(10) == [5, 4, 3, 2]
        with pytest.raises(ValueError):
            ring.last(-1)

    def test_empty_buffer(self) -> None:
        ring: CircularArray[int] = CircularArray(2)
        with pytest.raises(EmptyStructureError):
            ring.newest()
        with pytest.raises(EmptyStructureError):
            ring.oldest()
        ring.push(1)
        ring.clear()
        assert len(ring) == 0 and ring.to_list() == []

    def test_invalid_capacity(self) -> None:
        with pytest.raises(ValueError):
            CircularArray(0)

    def test_differential_against_a_sliding_window(self) -> None:
        rng = random.Random(9)
        ring: CircularArray[int] = CircularArray(7)
        window: list[int] = []
        for _ in range(500):
            value = rng.randint(0, 999)
            ring.push(value)
            window.append(value)
            window = window[-7:]
            assert ring.to_list() == window


# --------------------------------------------------------------------------------------
# Pointer safety of the doubly linked list and of the structures that keep node handles
# --------------------------------------------------------------------------------------

from core_service.alerts import AlertIndex  # noqa: E402
from core_service.clock import FakeClock  # noqa: E402
from core_service.data_structures import Node  # noqa: E402
from core_service.models import Direction, ProximityAlert  # noqa: E402
from core_service.security import SlidingWindowRateLimiter  # noqa: E402


def assert_consistent(dll: DoublyLinkedList) -> None:
    """Every pointer agrees: forward walk, backward walk, prev/next symmetry, owner and size."""
    forward = list(dll.nodes())
    backward = list(dll.nodes(reverse=True))
    assert forward == list(reversed(backward)) and len(forward) == len(dll)
    previous = None
    for node in forward:
        assert node.prev is previous and node.owner is dll
        previous = node
    assert dll.tail is previous
    assert (dll.head is None) == (len(dll) == 0)
    if dll.head is not None:
        assert dll.head.prev is None and dll.tail.next is None  # type: ignore[union-attr]


class TestListPointerSafety:
    def test_a_removed_node_is_dead_and_cannot_corrupt_the_list(self) -> None:
        dll = DoublyLinkedList([1, 2, 3])
        middle = dll.head.next  # type: ignore[union-attr]
        assert dll.remove(middle) == 2
        assert middle.prev is None and middle.next is None and middle.owner is None
        with pytest.raises(ValueError):
            dll.remove(middle)
        with pytest.raises(ValueError):
            dll.insert_after(middle, 9)
        assert not dll.contains(middle)
        assert dll.to_list() == [1, 3]
        assert_consistent(dll)

    def test_a_node_of_another_list_is_rejected(self) -> None:
        first, second = DoublyLinkedList([1]), DoublyLinkedList([2])
        with pytest.raises(ValueError):
            first.remove(second.head)  # type: ignore[arg-type]
        with pytest.raises(ValueError):
            first.move_to_front(second.head)  # type: ignore[arg-type]
        assert first.to_list() == [1] and second.to_list() == [2]

    def test_clear_invalidates_every_handle(self) -> None:
        dll = DoublyLinkedList([1, 2, 3])
        handles = list(dll.nodes())
        dll.clear()
        assert all(h.owner is None and h.prev is None and h.next is None for h in handles)
        with pytest.raises(ValueError):
            dll.remove(handles[1])
        dll.append(7)
        assert dll.to_list() == [7]
        assert_consistent(dll)

    @pytest.mark.parametrize("position", [0, 1, 2, 3])
    def test_move_to_front_keeps_the_handle_and_relinks_everything(self, position: int) -> None:
        dll = DoublyLinkedList(["a", "b", "c", "d"])
        handles = list(dll.nodes())
        dll.move_to_front(handles[position])
        expected = [handles[position].value] + [h.value for i, h in enumerate(handles) if i != position]
        assert dll.to_list() == expected and dll.head is handles[position]
        assert dll.contains(handles[position])
        assert_consistent(dll)

    @pytest.mark.parametrize("position", [0, 1, 2, 3])
    def test_move_to_back_keeps_the_handle_and_relinks_everything(self, position: int) -> None:
        dll = DoublyLinkedList(["a", "b", "c", "d"])
        handles = list(dll.nodes())
        dll.move_to_back(handles[position])
        expected = [h.value for i, h in enumerate(handles) if i != position] + [handles[position].value]
        assert dll.to_list() == expected and dll.tail is handles[position]
        assert_consistent(dll)

    def test_moving_in_a_single_element_list_is_a_no_op(self) -> None:
        dll = DoublyLinkedList([1])
        node = dll.head
        dll.move_to_front(node)  # type: ignore[arg-type]
        dll.move_to_back(node)  # type: ignore[arg-type]
        assert dll.to_list() == [1] and dll.head is dll.tail is node
        assert_consistent(dll)

    def test_removing_a_node_ahead_of_the_cursor_is_detected_instead_of_silently_truncating(self) -> None:
        dll = DoublyLinkedList([1, 2, 3, 4])
        seen: list[int] = []
        with pytest.raises(RuntimeError):
            for node in dll.nodes():
                seen.append(node.value)
                if node.value == 1:
                    dll.remove(node.next)  # type: ignore[arg-type]
        assert seen == [1]

    def test_removing_the_current_node_while_iterating_is_supported(self) -> None:
        dll = DoublyLinkedList(range(6))
        for node in dll.nodes():
            if node.value % 2 == 0:
                dll.remove(node)
        assert dll.to_list() == [1, 3, 5]
        assert_consistent(dll)

    @pytest.mark.parametrize("seed", range(20))
    def test_random_operations_match_a_python_list_model(self, seed: int) -> None:
        rng = random.Random(seed)
        dll: DoublyLinkedList[int] = DoublyLinkedList()
        model: list[Node[int]] = []
        for step in range(300):
            action = rng.choice(
                ["append", "prepend", "before", "after", "remove", "front", "back", "pop_front", "pop_back"]
            )
            if action == "append":
                model.append(dll.append(step))
            elif action == "prepend":
                model.insert(0, dll.prepend(step))
            elif model and action in {"before", "after", "remove", "front", "back"}:
                index = rng.randrange(len(model))
                node = model[index]
                if action == "before":
                    model.insert(index, dll.insert_before(node, step))
                elif action == "after":
                    model.insert(index + 1, dll.insert_after(node, step))
                elif action == "remove":
                    dll.remove(node)
                    model.pop(index)
                elif action == "front":
                    dll.move_to_front(node)
                    model.insert(0, model.pop(index))
                else:
                    dll.move_to_back(node)
                    model.append(model.pop(index))
            elif model and action == "pop_front":
                assert dll.pop_front() == model.pop(0).value
            elif model and action == "pop_back":
                assert dll.pop_back() == model.pop().value
            assert dll.to_list() == [n.value for n in model]
            assert_consistent(dll)


class TestStructuresHoldingNodeHandles:
    @staticmethod
    def alert(alert_id: str, stop_id: str = "S1") -> ProximityAlert:
        return ProximityAlert(alert_id, "device-1", "C5", Direction.OUTBOUND, stop_id, 2, 0.0, 3600.0)

    def test_alert_index_stays_consistent_through_add_replace_and_remove(self) -> None:
        index = AlertIndex()
        for number in range(5):
            index.add(self.alert(f"a{number}", "S1" if number % 2 == 0 else "S2"))
        index.add(self.alert("a0", "S2"))  # the same alert moves to another stop
        assert [a.alert_id for a in index.for_stop("S1")] == ["a2", "a4"]
        assert [a.alert_id for a in index.for_stop("S2")] == ["a1", "a3", "a0"]
        assert index.remove("a3") is not None and index.remove("a3") is None
        assert len(index) == 4 and {a.alert_id for a in index.all()} == {"a0", "a1", "a2", "a4"}
        for alert_id in ("a1", "a0", "a2", "a4"):
            index.remove(alert_id)
        assert len(index) == 0 and index.for_stop("S1") == [] and index.for_stop("S2") == []

    def test_replace_all_swaps_atomically_and_survives_duplicated_input(self) -> None:
        index = AlertIndex()
        index.add(self.alert("old"))
        index.replace_all([self.alert("a"), self.alert("b", "S2"), self.alert("a", "S2")])
        assert index.get("old") is None
        assert [a.alert_id for a in index.for_stop("S1")] == [] and len(index) == 2
        assert [a.alert_id for a in index.for_stop("S2")] == ["b", "a"]
        assert index.remove("a") is not None and [a.alert_id for a in index.for_stop("S2")] == ["b"]

    def test_lru_evicts_the_least_recently_used_key_and_keeps_handles_valid(self) -> None:
        clock = FakeClock(100.0)
        limiter = SlidingWindowRateLimiter(limit=2, window_seconds=60, clock=clock, max_keys=3)
        for key in ("a", "b", "c"):
            assert limiter.check(key).allowed
        assert limiter.check("a").allowed  # touching "a" makes "b" the oldest
        assert limiter.check("d").allowed  # evicts "b"
        assert len(limiter) == 3
        # "a" has used both of its slots and is still tracked (it was not evicted): third call is rejected
        assert not limiter.check("a").allowed
        # "b" was evicted, so it starts again with a full quota
        assert limiter.check("b").allowed and limiter.check("b").allowed and not limiter.check("b").allowed
        assert len(limiter) == 3
