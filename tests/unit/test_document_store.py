"""Non-relational document store: TTL, atomic operations, durability and crash tolerance."""

from __future__ import annotations

import threading
from pathlib import Path

import pytest

from core_service.clock import FakeClock
from core_service.errors import ValidationError
from core_service.persistence.documents import AppendOnlyDocumentStore, InMemoryDocumentStore


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock(1000.0)


class TestInMemoryStore:
    def test_put_get_delete_and_copies(self, clock: FakeClock) -> None:
        store = InMemoryDocumentStore(clock)
        original = {"a": 1, "nested": {"b": [1, 2]}}
        store.put("things", "k", original)
        original["a"] = 99  # mutating the source must not change the stored copy
        fetched = store.get("things", "k")
        assert fetched == {"a": 1, "nested": {"b": [1, 2]}}
        assert fetched is not None
        fetched["a"] = 5
        assert store.get("things", "k") == {"a": 1, "nested": {"b": [1, 2]}}
        assert store.delete("things", "k") is True
        assert store.delete("things", "k") is False
        assert store.get("things", "k") is None

    def test_rejects_non_serializable_documents(self, clock: FakeClock) -> None:
        with pytest.raises(ValidationError):
            InMemoryDocumentStore(clock).put("c", "k", {"x": object()})

    def test_ttl_expires_documents(self, clock: FakeClock) -> None:
        store = InMemoryDocumentStore(clock)
        store.put("c", "short", {"v": 1}, ttl_seconds=10)
        store.put("c", "forever", {"v": 2})
        clock.advance(9)
        assert store.exists("c", "short")
        clock.advance(2)
        assert store.get("c", "short") is None
        assert store.get("c", "forever") == {"v": 2}
        assert store.keys("c") == ["forever"]

    def test_invalid_ttl(self, clock: FakeClock) -> None:
        with pytest.raises(ValidationError):
            InMemoryDocumentStore(clock).put("c", "k", {}, ttl_seconds=0)

    def test_set_if_absent_is_first_writer_wins(self, clock: FakeClock) -> None:
        store = InMemoryDocumentStore(clock)
        assert store.set_if_absent("c", "k", {"who": "first"}, ttl_seconds=5) is True
        assert store.set_if_absent("c", "k", {"who": "second"}, ttl_seconds=5) is False
        assert store.get("c", "k") == {"who": "first"}
        clock.advance(6)  # expired keys are free again
        assert store.set_if_absent("c", "k", {"who": "third"}) is True

    def test_set_if_absent_is_atomic_across_threads(self, clock: FakeClock) -> None:
        store = InMemoryDocumentStore(clock)
        winners: list[int] = []

        def contender(number: int) -> None:
            if store.set_if_absent("locks", "job", {"n": number}):
                winners.append(number)

        threads = [threading.Thread(target=contender, args=(n,)) for n in range(40)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        assert len(winners) == 1

    def test_increment_keeps_the_original_ttl_window(self, clock: FakeClock) -> None:
        store = InMemoryDocumentStore(clock)
        assert store.increment("counters", "c", ttl_seconds=10) == 1
        clock.advance(6)
        assert store.increment("counters", "c", ttl_seconds=10) == 2
        assert store.increment("counters", "c", amount=3) == 5
        clock.advance(5)  # 11 s after the first increment: expired
        assert store.increment("counters", "c", ttl_seconds=10) == 1

    def test_find_with_predicate_and_purge(self, clock: FakeClock) -> None:
        store = InMemoryDocumentStore(clock)
        store.put("buses", "1", {"route": "C5"}, ttl_seconds=5)
        store.put("buses", "2", {"route": "C1"})
        store.put("buses", "3", {"route": "C5"})
        assert len(store.find("buses")) == 3
        assert {d["route"] for d in store.find("buses", lambda d: d["route"] == "C5")} == {"C5"}
        clock.advance(6)
        assert store.purge_expired() == 1  # only the document with a TTL
        assert store.purge_expired() == 0
        assert len(store.find("buses")) == 2

    def test_ping(self, clock: FakeClock) -> None:
        assert InMemoryDocumentStore(clock).ping() is True


class TestAppendOnlyStore:
    def test_documents_survive_a_restart(self, tmp_path: Path, clock: FakeClock) -> None:
        path = tmp_path / "docs.wal"
        first = AppendOnlyDocumentStore(path, clock)
        first.put("live_state", "1045", {"pct": 88})
        first.put("live_state", "1102", {"pct": 40})
        first.delete("live_state", "1102")
        first.close()

        second = AppendOnlyDocumentStore(path, clock)
        assert second.get("live_state", "1045") == {"pct": 88}
        assert second.get("live_state", "1102") is None
        second.close()

    def test_expired_documents_are_not_restored(self, tmp_path: Path, clock: FakeClock) -> None:
        path = tmp_path / "docs.wal"
        store = AppendOnlyDocumentStore(path, clock)
        store.put("c", "temp", {"v": 1}, ttl_seconds=10)
        store.put("c", "keep", {"v": 2})
        store.close()
        clock.advance(11)
        reopened = AppendOnlyDocumentStore(path, clock)
        assert reopened.get("c", "temp") is None
        assert reopened.get("c", "keep") == {"v": 2}
        reopened.close()

    def test_a_truncated_last_line_does_not_prevent_start_up(self, tmp_path: Path, clock: FakeClock) -> None:
        path = tmp_path / "docs.wal"
        store = AppendOnlyDocumentStore(path, clock)
        store.put("c", "ok", {"v": 1})
        store.close()
        with open(path, "a", encoding="utf-8") as handle:
            handle.write('{"op":"put","c":"c","k":"broken","p":"{\\"v\\":')  # crash mid-write
        recovered = AppendOnlyDocumentStore(path, clock)
        assert recovered.get("c", "ok") == {"v": 1}
        assert recovered.get("c", "broken") is None
        assert recovered.corrupt_lines == 1
        recovered.close()

    def test_compaction_shrinks_the_log_and_keeps_the_data(self, tmp_path: Path, clock: FakeClock) -> None:
        path = tmp_path / "docs.wal"
        store = AppendOnlyDocumentStore(path, clock, compact_min_lines=50, compact_ratio=2)
        for i in range(200):
            store.put("c", "same-key", {"i": i})
        store.close()
        lines = path.read_text(encoding="utf-8").splitlines()
        assert len(lines) < 200
        reopened = AppendOnlyDocumentStore(path, clock)
        assert reopened.get("c", "same-key") == {"i": 199}
        reopened.close()

    def test_writes_after_close_fail_loudly(self, tmp_path: Path, clock: FakeClock) -> None:
        store = AppendOnlyDocumentStore(tmp_path / "docs.wal", clock)
        store.close()
        with pytest.raises(RuntimeError):
            store.put("c", "k", {"v": 1})
