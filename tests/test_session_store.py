"""Tests for src/cache/session_store.py (owner: Claude -- fully built, not a
stub, so these are real assertions rather than an xfail spec).

Each test builds its own SessionStore against a temp file rather than the
process-global DB_PATH -- sharing state across tests is exactly the kind of
thing that would have hidden the schema/model coupling bug in Concept 16 if
this suite reused one instance everywhere.
"""

from __future__ import annotations

import time

import pytest

from src.cache.session_store import SessionStore


@pytest.fixture
def store(tmp_path):
    s = SessionStore(tmp_path / "test.db")
    yield s
    s.close()


# ---- session turns

def test_recent_turns_returns_most_recent_first(store):
    store.append_turn("s1", "revenue by category?", intent={"measure": "total_revenue"})
    store.append_turn("s1", "and by product instead?", intent={"measure": "total_revenue"})

    turns = store.recent_turns("s1")
    assert [t["question"] for t in turns] == ["and by product instead?", "revenue by category?"]


def test_turns_are_isolated_per_session(store):
    store.append_turn("s1", "q1")
    store.append_turn("s2", "q2")
    assert [t["question"] for t in store.recent_turns("s1")] == ["q1"]
    assert [t["question"] for t in store.recent_turns("s2")] == ["q2"]


def test_recent_turns_respects_limit(store):
    for i in range(5):
        store.append_turn("s1", f"q{i}")
    assert len(store.recent_turns("s1", limit=2)) == 2


def test_turn_with_no_intent_round_trips_as_none(store):
    store.append_turn("s1", "an unsupported question")
    assert store.recent_turns("s1")[0]["intent"] is None


# ---- semantic cache

def test_cache_miss_returns_none(store):
    assert store.cache_get("nonexistent-key") is None


def test_cache_set_then_get_round_trips(store):
    store.cache_set(
        "k1",
        rows=[{"total_revenue": 123.45}],
        compiled_sql="SELECT 1",
        ttl_seconds=60,
        metadata={"route_taken": "compiled"},
    )
    entry = store.cache_get("k1")
    assert entry is not None
    assert entry.rows == [{"total_revenue": 123.45}]
    assert entry.compiled_sql == "SELECT 1"
    assert entry.metadata == {"route_taken": "compiled"}


def test_expired_entry_is_treated_as_a_miss(store):
    store.cache_set("k1", rows=[], compiled_sql="SELECT 1", ttl_seconds=-1)
    assert store.cache_get("k1") is None


def test_cache_set_overwrites_existing_entry(store):
    store.cache_set("k1", rows=[{"a": 1}], compiled_sql="SELECT 1", ttl_seconds=60)
    store.cache_set("k1", rows=[{"a": 2}], compiled_sql="SELECT 2", ttl_seconds=60)
    entry = store.cache_get("k1")
    assert entry.rows == [{"a": 2}]
    assert entry.compiled_sql == "SELECT 2"


def test_cache_invalidate_removes_the_entry_and_reports_it_did(store):
    store.cache_set("k1", rows=[], compiled_sql="SELECT 1", ttl_seconds=60)
    assert store.cache_invalidate("k1") is True
    assert store.cache_get("k1") is None


def test_cache_invalidate_on_missing_key_reports_nothing_removed(store):
    assert store.cache_invalidate("never-existed") is False


def test_purge_expired_removes_only_expired_rows(store):
    store.cache_set("expired", rows=[], compiled_sql="SELECT 1", ttl_seconds=-1)
    store.cache_set("fresh", rows=[], compiled_sql="SELECT 2", ttl_seconds=60)
    removed = store.purge_expired()
    assert removed == 1
    assert store.cache_get("expired") is None
    assert store.cache_get("fresh") is not None


# ---- thread safety / WAL mode

def test_concurrent_writes_from_multiple_threads_do_not_corrupt_the_store(store, tmp_path):
    import threading

    def writer(i):
        for j in range(10):
            store.cache_set(f"k-{i}-{j}", rows=[{"i": i, "j": j}], compiled_sql="SELECT 1", ttl_seconds=60)

    threads = [threading.Thread(target=writer, args=(i,)) for i in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    for i in range(5):
        for j in range(10):
            entry = store.cache_get(f"k-{i}-{j}")
            assert entry is not None
            assert entry.rows == [{"i": i, "j": j}]


def test_wal_mode_is_actually_enabled(store):
    cursor = store._conn.execute("PRAGMA journal_mode")
    assert cursor.fetchone()[0].lower() == "wal"
