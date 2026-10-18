"""Redis layer: document store parity, distributed locks, shared rate limits and cluster-wide state.

Every test runs on fakeredis and, when ``CORE_TEST_REDIS_URL`` is set, again on a real Redis.
"""

from __future__ import annotations

import threading
import time
from typing import Any

import pytest
import redis

from core_service.alerts import AlertIndex, RedisIndexReplicator
from core_service.auth import LoginAttemptTracker
from core_service.clock import FakeClock
from core_service.errors import ServiceUnavailableError, ValidationError
from core_service.metrics import MetricsRegistry
from core_service.models import Direction, ProximityAlert
from core_service.persistence.documents import (
    InMemoryDocumentStore,
    RedisDocumentStore,
    RedisOptions,
    build_redis_client,
    parse_sentinels,
)
from core_service.security import RedisSlidingWindowRateLimiter, ResilientRateLimiter, SlidingWindowRateLimiter
from core_service.services.jobs import JobRunner
from core_service.services.keyed_locks import RedisLockManager


@pytest.fixture
def store(redis_client: Any) -> RedisDocumentStore:
    return RedisDocumentStore(redis_client, prefix="t")


@pytest.fixture
def dead_client() -> Any:
    """A client pointing at a port where nothing listens: every call fails fast."""
    return redis.Redis(host="127.0.0.1", port=1, socket_connect_timeout=0.2, socket_timeout=0.2, decode_responses=True)


class TestRedisDocumentStore:
    def test_put_get_delete_and_copies(self, store: RedisDocumentStore) -> None:
        original = {"a": 1, "nested": {"b": [1, 2]}, "text": "ñandú"}
        store.put("things", "k", original)
        original["a"] = 99
        assert store.get("things", "k") == {"a": 1, "nested": {"b": [1, 2]}, "text": "ñandú"}
        assert store.delete("things", "k") is True and store.delete("things", "k") is False
        assert store.get("things", "k") is None and not store.exists("things", "k")

    def test_rejects_non_serializable_documents(self, store: RedisDocumentStore) -> None:
        with pytest.raises(ValidationError):
            store.put("c", "k", {"x": object()})

    def test_ttl_is_enforced_by_redis(self, store: RedisDocumentStore) -> None:
        store.put("c", "short", {"v": 1}, ttl_seconds=0.15)
        store.put("c", "forever", {"v": 2})
        assert store.exists("c", "short")
        time.sleep(0.3)
        assert store.get("c", "short") is None and store.keys("c") == ["forever"]

    def test_invalid_ttl(self, store: RedisDocumentStore) -> None:
        with pytest.raises(ValidationError):
            store.put("c", "k", {}, ttl_seconds=0)

    def test_set_if_absent_is_first_writer_wins_even_across_threads(self, store: RedisDocumentStore) -> None:
        assert store.set_if_absent("c", "k", {"who": "first"}, ttl_seconds=5) is True
        assert store.set_if_absent("c", "k", {"who": "second"}) is False
        assert store.get("c", "k") == {"who": "first"}
        winners: list[int] = []

        def contender(number: int) -> None:
            if store.set_if_absent("locks", "job", {"n": number}):
                winners.append(number)

        threads = [threading.Thread(target=contender, args=(n,)) for n in range(30)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        assert len(winners) == 1

    def test_increment_is_atomic_and_the_ttl_window_is_fixed_at_creation(self, store: RedisDocumentStore) -> None:
        assert store.increment("counters", "c", ttl_seconds=0.4) == 1
        time.sleep(0.2)
        assert store.increment("counters", "c", ttl_seconds=0.4) == 2  # does not extend the window
        assert store.increment("counters", "c", amount=3) == 5
        time.sleep(0.3)  # 0.5 s after creation: expired
        assert store.increment("counters", "c", ttl_seconds=0.4) == 1

    def test_concurrent_increments_lose_nothing(self, store: RedisDocumentStore) -> None:
        threads = [threading.Thread(target=lambda: [store.increment("n", "c") for _ in range(25)]) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        assert store.increment("n", "c", amount=0) == 200

    def test_find_keys_and_predicates_ignore_counters_and_other_collections(self, store: RedisDocumentStore) -> None:
        store.put("buses", "1", {"route": "C5"})
        store.put("buses", "2", {"route": "C1"})
        store.put("buses", "3", {"route": "C5"})
        store.put("buses_other", "9", {"route": "C5"})
        store.increment("buses", "counter")
        assert len(store.find("buses")) == 3 and store.keys("buses") == ["1", "2", "3"]
        assert {d["route"] for d in store.find("buses", lambda d: d["route"] == "C5")} == {"C5"}

    def test_collection_names_with_glob_characters_do_not_leak_between_collections(
        self, store: RedisDocumentStore
    ) -> None:
        store.put("a*b", "1", {"who": "star"})
        store.put("axb", "1", {"who": "x"})
        store.put("a[b]", "1", {"who": "brackets"})
        assert [d["who"] for d in store.find("a*b")] == ["star"]
        assert [d["who"] for d in store.find("a[b]")] == ["brackets"]

    def test_find_handles_more_documents_than_one_scan_batch(self, store: RedisDocumentStore) -> None:
        for number in range(1200):
            store.put("bulk", str(number), {"n": number})
        assert len(store.find("bulk")) == 1200 and len(store.keys("bulk")) == 1200

    def test_sets(self, store: RedisDocumentStore) -> None:
        assert store.add_to_set("idx", "C5", "1045") is True and store.add_to_set("idx", "C5", "1045") is False
        store.add_to_set("idx", "C5", "1102")
        assert store.set_members("idx", "C5") == {"1045", "1102"} and store.set_members("idx", "C1") == set()
        assert (
            store.remove_from_set("idx", "C5", "1045") is True and store.remove_from_set("idx", "C5", "1045") is False
        )
        assert store.set_members("idx", "C5") == {"1102"}

    def test_purge_and_ping(self, store: RedisDocumentStore) -> None:
        assert store.purge_expired() == 0 and store.ping() is True

    def test_a_redis_outage_is_reported_as_service_unavailable(self, dead_client: Any) -> None:
        broken = RedisDocumentStore(dead_client)
        with pytest.raises(ServiceUnavailableError):
            broken.get("c", "k")
        with pytest.raises(ServiceUnavailableError):
            broken.put("c", "k", {})
        with pytest.raises(ServiceUnavailableError):
            broken.increment("c", "k")
        with pytest.raises(ServiceUnavailableError):
            broken.find("c")
        assert broken.ping() is False


class TestClientFactory:
    def test_sentinel_addresses_are_parsed_and_validated(self) -> None:
        assert parse_sentinels("a:26379, b:26380") == (("a", 26379), ("b", 26380))
        assert parse_sentinels("") == ()
        with pytest.raises(ValueError):
            parse_sentinels("no-port")

    def test_a_url_or_sentinels_are_required(self) -> None:
        with pytest.raises(ValueError):
            build_redis_client(RedisOptions())

    def test_the_client_has_timeouts_and_health_checks(self) -> None:
        client = build_redis_client(RedisOptions(url="redis://localhost:6379/0", socket_timeout_seconds=1.5))
        kwargs = client.connection_pool.connection_kwargs
        assert kwargs["socket_timeout"] == 1.5 and kwargs["health_check_interval"] > 0
        assert kwargs["decode_responses"] is True

    def test_sentinel_mode_builds_a_master_client(self) -> None:
        client = build_redis_client(RedisOptions(sentinels=(("s1", 26379), ("s2", 26379)), sentinel_master="prod"))
        assert client.connection_pool.service_name == "prod"  # type: ignore[attr-defined]


class TestRedisLockManager:
    def test_mutual_exclusion_between_two_replicas(self, redis_client: Any) -> None:
        replicas = [RedisLockManager(redis_client, "t"), RedisLockManager(redis_client, "t")]
        inside, overlaps, counter = [0], [0], [0]
        guard = threading.Lock()

        def work(manager: RedisLockManager) -> None:
            for _ in range(15):
                with manager.acquire("bus:1045"):
                    with guard:
                        inside[0] += 1
                        overlaps[0] += inside[0] > 1
                    value = counter[0]
                    time.sleep(0.0005)
                    counter[0] = value + 1  # lost updates would show here
                    with guard:
                        inside[0] -= 1

        threads = [threading.Thread(target=work, args=(replicas[i % 2],)) for i in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        assert overlaps[0] == 0 and counter[0] == 60

    def test_different_keys_do_not_block_each_other(self, redis_client: Any) -> None:
        first, second = RedisLockManager(redis_client, "t"), RedisLockManager(redis_client, "t", wait_seconds=0.2)
        with first.acquire("a"), second.acquire("b"):
            pass

    def test_the_lock_is_reentrant_inside_one_thread(self, redis_client: Any) -> None:
        manager = RedisLockManager(redis_client, "t", wait_seconds=0.2)
        with manager.acquire("k"), manager.acquire("k"):
            assert redis_client.exists("t:lock:k") == 1
        assert redis_client.exists("t:lock:k") == 0

    def test_waiting_too_long_is_an_error_not_a_deadlock(self, redis_client: Any) -> None:
        holder = RedisLockManager(redis_client, "t")
        waiter = RedisLockManager(redis_client, "t", wait_seconds=0.15, poll_seconds=0.01)
        with holder.acquire("k"), pytest.raises(ServiceUnavailableError), waiter.acquire("k"):
            pass

    def test_a_holder_that_dies_cannot_block_forever(self, redis_client: Any) -> None:
        slow = RedisLockManager(redis_client, "t", ttl_seconds=0.1)
        other = RedisLockManager(redis_client, "t", wait_seconds=1.0, poll_seconds=0.01)
        with slow.acquire("k"):
            time.sleep(0.2)  # the lease expires while "slow" is still (or no longer) working
            started = time.monotonic()
            with other.acquire("k"):  # taken immediately: the expired lease was not waited for
                assert time.monotonic() - started < 0.15

    def test_a_stale_token_never_deletes_somebody_elses_lock(self, redis_client: Any) -> None:
        manager = RedisLockManager(redis_client, "t", ttl_seconds=0.1)
        with manager.acquire("k"):
            time.sleep(0.2)
            redis_client.set("t:lock:k", "someone-else", px=5000)
        assert redis_client.get("t:lock:k") == "someone-else"

    def test_a_redis_outage_is_reported_as_service_unavailable(self, dead_client: Any) -> None:
        manager = RedisLockManager(dead_client, "t", wait_seconds=0.2)
        with pytest.raises(ServiceUnavailableError), manager.acquire("k"):
            pass
        with pytest.raises(ServiceUnavailableError), manager.acquire("k"):  # the failed attempt left no local state
            pass


class TestSharedRateLimiter:
    def test_the_quota_is_shared_by_every_replica(self, redis_client: Any) -> None:
        clock = FakeClock(1_000.0)
        replica_a = RedisSlidingWindowRateLimiter(redis_client, 3, 60.0, clock, "t")
        replica_b = RedisSlidingWindowRateLimiter(redis_client, 3, 60.0, clock, "t")
        results = [replica_a.check("ip"), replica_b.check("ip"), replica_a.check("ip"), replica_b.check("ip")]
        assert [r.allowed for r in results] == [True, True, True, False]
        assert results[0].remaining == 2 and results[2].remaining == 0
        assert results[3].retry_after_seconds == pytest.approx(60.0, abs=0.01)
        assert replica_a.check("other-ip").allowed  # keys are independent

    def test_the_window_slides(self, redis_client: Any) -> None:
        clock = FakeClock(1_000.0)
        limiter = RedisSlidingWindowRateLimiter(redis_client, 2, 10.0, clock, "t")
        assert limiter.check("k").allowed
        clock.advance(6)
        assert limiter.check("k").allowed
        assert not limiter.check("k").allowed
        clock.advance(5)  # the first request (t=1000) left the window; the second (t=1006) has not
        assert limiter.check("k").allowed and not limiter.check("k").allowed

    def test_same_instant_requests_are_all_counted(self, redis_client: Any) -> None:
        limiter = RedisSlidingWindowRateLimiter(redis_client, 5, 10.0, FakeClock(1_000.0), "t")
        assert [limiter.check("k").allowed for _ in range(6)] == [True] * 5 + [False]

    def test_it_agrees_with_the_in_process_limiter(self, redis_client: Any) -> None:
        clock = FakeClock(50.0)
        shared = RedisSlidingWindowRateLimiter(redis_client, 4, 20.0, clock, "t")
        local = SlidingWindowRateLimiter(4, 20.0, clock)
        for step in range(40):
            clock.advance(1.5)
            assert shared.check("k").allowed == local.check("k").allowed, f"diverged at step {step}"

    def test_the_service_keeps_limiting_locally_when_redis_is_down(self, dead_client: Any) -> None:
        clock = FakeClock(1_000.0)
        limiter = ResilientRateLimiter(
            RedisSlidingWindowRateLimiter(dead_client, 2, 60.0, clock), SlidingWindowRateLimiter(2, 60.0, clock)
        )
        assert [limiter.check("k").allowed for _ in range(3)] == [True, True, False]
        with pytest.raises(ServiceUnavailableError):
            RedisSlidingWindowRateLimiter(dead_client, 2, 60.0, clock).check("k")


class TestClusterWideState:
    def test_login_lockout_is_shared_and_does_not_store_e_mails(self, store: RedisDocumentStore) -> None:
        clock = FakeClock(1_000.0)
        replica_a = LoginAttemptTracker(clock, 3, 300.0, store)
        replica_b = LoginAttemptTracker(clock, 3, 300.0, store)
        replica_a.record_failure("Ana@Example.com")
        replica_b.record_failure("ana@example.com")
        assert replica_a.lock_remaining("ana@example.com") is None
        replica_a.record_failure("ana@example.com ")
        assert replica_b.lock_remaining("ANA@example.com") == pytest.approx(300.0, abs=0.01)  # locked on BOTH
        assert all("@" not in key and "ana" not in key.lower() for key in store.keys("login_failures"))
        replica_b.record_success("ana@example.com")
        assert replica_a.lock_remaining("ana@example.com") is None

    def test_login_lockout_expires_from_the_store_by_itself(self, store: RedisDocumentStore) -> None:
        tracker = LoginAttemptTracker(FakeClock(1_000.0), 3, 0.1, store)
        for _ in range(3):
            tracker.record_failure("ana@example.com")
        assert len(store.keys("login_failures")) == 1
        time.sleep(0.35)  # the document lives 2 x lock_seconds
        assert store.keys("login_failures") == []

    def test_a_redis_lease_lets_only_one_replica_run_the_job(self, store: RedisDocumentStore) -> None:
        clock, calls = FakeClock(1_000.0), {"a": 0, "b": 0}
        runner_a = JobRunner(clock, MetricsRegistry(), coordinator=store, node_id="a")
        runner_b = JobRunner(clock, MetricsRegistry(), coordinator=store, node_id="b")
        runner_a.add("purge", 60, lambda: calls.__setitem__("a", calls["a"] + 1))
        runner_b.add("purge", 60, lambda: calls.__setitem__("b", calls["b"] + 1))
        clock.advance(60)
        runner_a.run_pending()
        runner_b.run_pending()
        assert calls["a"] + calls["b"] == 1  # the lease (a Redis TTL of 48 s) is still held by the winner

    def test_a_lease_that_cannot_be_taken_skips_the_round_instead_of_running_twice(self) -> None:
        clock = FakeClock(1_000.0)
        shared = InMemoryDocumentStore(clock)
        runs: list[str] = []
        first = JobRunner(clock, MetricsRegistry(), coordinator=shared, node_id="a")
        second = JobRunner(clock, MetricsRegistry(), coordinator=shared, node_id="b")
        first.add("digest", 60, lambda: runs.append("a"))
        second.add("digest", 60, lambda: runs.append("b"))
        for _ in range(4):
            clock.advance(60)
            first.run_pending()
            second.run_pending()
        assert len(runs) == 4  # exactly one replica per round
        assert set(runs) <= {"a", "b"}

    def test_a_coordinator_outage_skips_the_round_without_crashing(self, dead_client: Any) -> None:
        clock, runs = FakeClock(1_000.0), []
        runner = JobRunner(clock, MetricsRegistry(), coordinator=RedisDocumentStore(dead_client))
        runner.add("digest", 60, lambda: runs.append(1))
        clock.advance(60)
        assert runner.run_pending() == [] and runs == []


class TestAlertIndexReplication:
    @staticmethod
    def alert(alert_id: str, stop_id: str = "P-022") -> ProximityAlert:
        return ProximityAlert(alert_id, "device-1", "C5", Direction.OUTBOUND, stop_id, 2, 0.0, 3600.0)

    def test_changes_reach_the_other_replicas_and_are_not_applied_twice_on_the_origin(self, redis_client: Any) -> None:
        index_a, index_b = AlertIndex(), AlertIndex()
        a = RedisIndexReplicator(redis_client, index_a, "t")
        b = RedisIndexReplicator(redis_client, index_b, "t")
        messages: list[str] = []
        pubsub = redis_client.pubsub(ignore_subscribe_messages=True)
        pubsub.subscribe("t:alert-index")
        a.announce_added(self.alert("x"))
        a.announce_removed("y")
        deadline = time.monotonic() + 3
        while len(messages) < 2 and time.monotonic() < deadline:
            message = pubsub.get_message(timeout=0.2)
            if message and message["type"] == "message":
                messages.append(message["data"])
        assert len(messages) == 2
        assert [b.handle(m) for m in messages] == [
            True,
            False,
        ]  # add applied; removing an unknown alert changes nothing
        assert index_b.get("x") is not None
        assert [a.handle(m) for m in messages] == [False, False]  # own messages are ignored
        pubsub.close()

    def test_the_background_subscription_keeps_replicas_in_step(self, redis_client: Any) -> None:
        index_a, index_b = AlertIndex(), AlertIndex()
        resynced: list[int] = []
        a = RedisIndexReplicator(redis_client, index_a, "t")
        b = RedisIndexReplicator(redis_client, index_b, "t", on_resync=lambda: resynced.append(1))
        b.start()
        try:
            deadline = time.monotonic() + 3
            while not resynced and time.monotonic() < deadline:
                time.sleep(0.02)
            assert resynced, "the subscriber reconciles with the database right after subscribing"
            a.announce_added(self.alert("x"))
            while index_b.get("x") is None and time.monotonic() < deadline:
                time.sleep(0.02)
            assert index_b.get("x") is not None
            a.announce_removed("x")
            while index_b.get("x") is not None and time.monotonic() < deadline:
                time.sleep(0.02)
            assert index_b.get("x") is None
        finally:
            b.stop()

    def test_malformed_messages_are_ignored(self, redis_client: Any) -> None:
        replicator = RedisIndexReplicator(redis_client, AlertIndex(), "t")
        for junk in ("not json", "{}", '{"op": "add"}', '{"op": "remove"}', "[]", "null"):
            assert replicator.handle(junk) is False

    def test_a_failed_broadcast_never_breaks_the_caller(self, dead_client: Any) -> None:
        metrics = MetricsRegistry()
        replicator = RedisIndexReplicator(dead_client, AlertIndex(), "t", metrics=metrics)
        replicator.announce_added(self.alert("x"))  # must not raise
        replicator.announce_removed("x")
