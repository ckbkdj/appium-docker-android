from __future__ import annotations

import asyncio
import json
import sqlite3
import time
from pathlib import Path

from .models import PrimitiveAction


class ActionCache:
    """SQLite-backed next-action cache keyed by goal, package and compact UI state."""

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = asyncio.Lock()
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=5)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=NORMAL")
        return connection

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS action_cache (
                    goal_key TEXT NOT NULL,
                    package TEXT NOT NULL,
                    state_hash TEXT NOT NULL,
                    action_json TEXT NOT NULL,
                    successes INTEGER NOT NULL DEFAULT 0,
                    failures INTEGER NOT NULL DEFAULT 0,
                    avg_latency_ms REAL NOT NULL DEFAULT 0,
                    updated_at_ms INTEGER NOT NULL,
                    PRIMARY KEY (goal_key, package, state_hash)
                );
                CREATE INDEX IF NOT EXISTS idx_action_cache_updated
                    ON action_cache(updated_at_ms DESC);
                """
            )

    async def get(
        self,
        goal_key: str,
        package: str,
        state_hash: str,
        *,
        min_successes: int = 1,
    ) -> PrimitiveAction | None:
        async with self._lock:
            with self._connect() as connection:
                row = connection.execute(
                    """
                    SELECT action_json, successes, failures
                    FROM action_cache
                    WHERE goal_key=? AND package=? AND state_hash=?
                    """,
                    (goal_key, package, state_hash),
                ).fetchone()
        if not row or row["successes"] < min_successes or row["failures"] > row["successes"]:
            return None
        return PrimitiveAction.model_validate(json.loads(row["action_json"]))

    async def record_success(
        self,
        goal_key: str,
        package: str,
        state_hash: str,
        action: PrimitiveAction,
        latency_ms: int,
    ) -> None:
        now = int(time.time() * 1000)
        payload = json.dumps(action.model_dump(mode="json"), ensure_ascii=False, separators=(",", ":"))
        async with self._lock:
            with self._connect() as connection:
                connection.execute(
                    """
                    INSERT INTO action_cache(
                        goal_key, package, state_hash, action_json,
                        successes, failures, avg_latency_ms, updated_at_ms
                    ) VALUES (?, ?, ?, ?, 1, 0, ?, ?)
                    ON CONFLICT(goal_key, package, state_hash) DO UPDATE SET
                        action_json=excluded.action_json,
                        successes=action_cache.successes + 1,
                        avg_latency_ms=(
                            action_cache.avg_latency_ms * action_cache.successes + excluded.avg_latency_ms
                        ) / (action_cache.successes + 1),
                        updated_at_ms=excluded.updated_at_ms
                    """,
                    (goal_key, package, state_hash, payload, latency_ms, now),
                )

    async def record_failure(self, goal_key: str, package: str, state_hash: str) -> None:
        async with self._lock:
            with self._connect() as connection:
                connection.execute(
                    """
                    UPDATE action_cache
                    SET failures=failures + 1, updated_at_ms=?
                    WHERE goal_key=? AND package=? AND state_hash=?
                    """,
                    (int(time.time() * 1000), goal_key, package, state_hash),
                )
