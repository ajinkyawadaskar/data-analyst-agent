"""SQLite-backed storage for Layer 4: multi-turn session history and the
semantic result cache.

WHY SQLITE, NOT REDIS
----------------------
Redis is the default answer for "I need a cache," but it is an extra service
to run, monitor, and explain the operational cost of, for a workload that is
a single-process FastAPI app with no horizontal scaling story yet. SQLite in
WAL (Write-Ahead Log) mode gives concurrent readers without blocking a
writer, survives a process restart (unlike an in-memory dict), and is one
file with zero infrastructure -- the honest answer to "why not Redis" in an
interview is "because nothing about this system's scale needs it yet, and
adding it would be solving a problem this project doesn't have."

TWO RESPONSIBILITIES, ONE FILE
--------------------------------
1. SESSION TURNS -- a record of what was asked and what intent it resolved
   to, per session_id. This is the substrate a future multi-turn feature
   would read ("what did 'that' refer to in the last question"); nothing
   in this build resolves pronouns yet, but the storage exists so that
   capability is additive later rather than a schema migration.

2. THE SEMANTIC CACHE -- rows keyed by intent hash (src/cache/intent_hash.py,
   Ajinkya's), so two differently-phrased-but-identical questions hit the
   same entry and skip BigQuery entirely. Cache the ROWS, not just the SQL --
   caching the SQL and still executing it is not a latency win.

THREAD SAFETY
--------------
FastAPI runs sync request handlers in a thread pool, so this module opens
its SQLite connection with check_same_thread=False and serializes writes
behind a single lock. WAL mode still lets concurrent reads proceed without
waiting on that lock -- only writers contend with each other, which is the
right tradeoff for a cache that is read far more often than it is written.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

DB_PATH = Path(__file__).resolve().parents[2] / "data" / "session_store.db"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS turns (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    question TEXT NOT NULL,
    intent_json TEXT,
    route_taken TEXT,
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_turns_session ON turns(session_id, created_at);

CREATE TABLE IF NOT EXISTS metric_cache (
    cache_key TEXT PRIMARY KEY,
    rows_json TEXT NOT NULL,
    compiled_sql TEXT NOT NULL,
    metadata_json TEXT NOT NULL,
    created_at REAL NOT NULL,
    expires_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_cache_expires ON metric_cache(expires_at);
"""


@dataclass(frozen=True)
class CacheEntry:
    """A cache hit, with everything the caller needs to reconstruct a
    response without re-running the pipeline."""

    rows: list[dict[str, Any]]
    compiled_sql: str
    metadata: dict[str, Any]
    cached_at: float


class SessionStore:
    """One SQLite connection per instance; safe to share across a FastAPI
    thread pool because every write goes through `self._lock`.

    Not a singleton by design -- tests construct their own instance against
    a temp path (see tests/test_session_store.py) rather than sharing
    process-global state, which is what made the schema/model validation
    bug in Concept 16 possible to catch in isolation in the first place.
    """

    def __init__(self, db_path: Path | str = DB_PATH) -> None:
        self._path = Path(db_path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(str(self._path), check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        with self._lock:
            self._conn.executescript(_SCHEMA)
            self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    # ---- session turns ---------------------------------------------------

    def append_turn(
        self,
        session_id: str,
        question: str,
        *,
        intent: dict | None = None,
        route_taken: str | None = None,
    ) -> None:
        """Record one turn. Fire-and-forget from the caller's perspective --
        a failure to record history should never fail the actual answer, so
        callers are expected to wrap this in their own best-effort handling
        if they call it inline on the request path."""
        with self._lock:
            self._conn.execute(
                "INSERT INTO turns (session_id, question, intent_json, "
                "route_taken, created_at) VALUES (?, ?, ?, ?, ?)",
                (
                    session_id,
                    question,
                    json.dumps(intent) if intent is not None else None,
                    route_taken,
                    time.time(),
                ),
            )
            self._conn.commit()

    def recent_turns(self, session_id: str, limit: int = 10) -> list[dict[str, Any]]:
        """Most recent turns first -- the shape a future "what did that
        refer to" resolver would want, without this module deciding
        anything about how that resolution works."""
        with self._lock:
            cursor = self._conn.execute(
                "SELECT question, intent_json, route_taken, created_at "
                "FROM turns WHERE session_id = ? ORDER BY created_at DESC LIMIT ?",
                (session_id, limit),
            )
            rows = cursor.fetchall()
        return [
            {
                "question": q,
                "intent": json.loads(i) if i else None,
                "route_taken": r,
                "created_at": c,
            }
            for q, i, r, c in rows
        ]

    # ---- semantic cache ----------------------------------------------------

    def cache_get(self, cache_key: str) -> CacheEntry | None:
        """None on a miss OR an expired entry -- the caller cannot tell the
        two apart, which is correct: both mean "run the pipeline." An
        expired row is lazily deleted on the next cache_set/purge_expired
        rather than on every read, to keep reads cheap."""
        with self._lock:
            cursor = self._conn.execute(
                "SELECT rows_json, compiled_sql, metadata_json, created_at, "
                "expires_at FROM metric_cache WHERE cache_key = ?",
                (cache_key,),
            )
            row = cursor.fetchone()
        if row is None:
            return None
        rows_json, compiled_sql, metadata_json, created_at, expires_at = row
        if expires_at < time.time():
            return None
        return CacheEntry(
            rows=json.loads(rows_json),
            compiled_sql=compiled_sql,
            metadata=json.loads(metadata_json),
            cached_at=created_at,
        )

    def cache_set(
        self,
        cache_key: str,
        rows: list[dict[str, Any]],
        compiled_sql: str,
        *,
        ttl_seconds: float,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        """Overwrites any existing entry for this key -- a fresh write always
        wins, so re-running a stale-but-not-yet-expired query for any reason
        (a manual invalidation workflow, a forced refresh) just works."""
        now = time.time()
        with self._lock:
            self._conn.execute(
                "INSERT INTO metric_cache (cache_key, rows_json, compiled_sql, "
                "metadata_json, created_at, expires_at) VALUES (?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(cache_key) DO UPDATE SET rows_json=excluded.rows_json, "
                "compiled_sql=excluded.compiled_sql, metadata_json=excluded.metadata_json, "
                "created_at=excluded.created_at, expires_at=excluded.expires_at",
                (
                    cache_key,
                    json.dumps(rows),
                    compiled_sql,
                    json.dumps(metadata or {}),
                    now,
                    now + ttl_seconds,
                ),
            )
            self._conn.commit()

    def cache_invalidate(self, cache_key: str) -> bool:
        """Manual invalidation path -- an unbounded cache with no way to
        force a refresh is a red flag, not a feature. Returns whether a row
        was actually deleted, so a caller can tell "invalidated" from
        "nothing to invalidate.\""""
        with self._lock:
            cursor = self._conn.execute(
                "DELETE FROM metric_cache WHERE cache_key = ?", (cache_key,)
            )
            self._conn.commit()
        return cursor.rowcount > 0

    def purge_expired(self) -> int:
        """Housekeeping: delete every entry whose TTL has passed. Not called
        automatically on every read (see cache_get) -- intended to run
        periodically (a scheduled task, or once per process start) so the
        database does not grow unbounded with dead rows."""
        with self._lock:
            cursor = self._conn.execute(
                "DELETE FROM metric_cache WHERE expires_at < ?", (time.time(),)
            )
            self._conn.commit()
        return cursor.rowcount
