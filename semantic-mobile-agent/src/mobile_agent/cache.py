from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

import aiosqlite

from .models import Plan


class WorkflowCache:
    def __init__(self, path: Path, min_successes: int = 2) -> None:
        self.path = path
        self.min_successes = min_successes

    async def initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        async with aiosqlite.connect(self.path) as db:
            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS workflow_cache (
                    cache_key TEXT PRIMARY KEY,
                    app TEXT,
                    state_hash TEXT,
                    instruction_hash TEXT NOT NULL,
                    plan_json TEXT NOT NULL,
                    success_count INTEGER NOT NULL DEFAULT 0,
                    failure_count INTEGER NOT NULL DEFAULT 0,
                    avg_latency_ms REAL NOT NULL DEFAULT 0,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            await db.commit()

    @staticmethod
    def instruction_hash(instruction: str) -> str:
        normalized = "".join(instruction.casefold().split())
        return hashlib.blake2s(normalized.encode("utf-8"), digest_size=12).hexdigest()

    def key(self, instruction: str, app: str | None, state_hash: str | None) -> str:
        payload = "|".join([self.instruction_hash(instruction), app or "", state_hash or ""])
        return hashlib.blake2s(payload.encode("utf-8"), digest_size=16).hexdigest()

    async def get(self, instruction: str, app: str | None, state_hash: str | None) -> Plan | None:
        cache_key = self.key(instruction, app, state_hash)
        async with aiosqlite.connect(self.path) as db:
            row = await (
                await db.execute(
                    "SELECT plan_json, success_count, failure_count FROM workflow_cache WHERE cache_key = ?",
                    (cache_key,),
                )
            ).fetchone()
        if not row:
            return None
        plan_json, successes, failures = row
        if successes < self.min_successes or failures > 0:
            return None
        plan = Plan.model_validate_json(plan_json)
        return plan.model_copy(update={"source": "cache", "confidence": 0.99})

    async def record_success(
        self,
        instruction: str,
        app: str | None,
        state_hash: str | None,
        plan: Plan,
        latency_ms: float,
    ) -> None:
        cache_key = self.key(instruction, app, state_hash)
        async with aiosqlite.connect(self.path) as db:
            await db.execute(
                """
                INSERT INTO workflow_cache (
                    cache_key, app, state_hash, instruction_hash, plan_json,
                    success_count, failure_count, avg_latency_ms
                ) VALUES (?, ?, ?, ?, ?, 1, 0, ?)
                ON CONFLICT(cache_key) DO UPDATE SET
                    plan_json = excluded.plan_json,
                    success_count = workflow_cache.success_count + 1,
                    avg_latency_ms = (
                        workflow_cache.avg_latency_ms * workflow_cache.success_count + excluded.avg_latency_ms
                    ) / (workflow_cache.success_count + 1),
                    updated_at = CURRENT_TIMESTAMP
                """,
                (
                    cache_key,
                    app,
                    state_hash,
                    self.instruction_hash(instruction),
                    plan.model_dump_json(),
                    latency_ms,
                ),
            )
            await db.commit()

    async def record_failure(
        self,
        instruction: str,
        app: str | None,
        state_hash: str | None,
        plan: Plan,
    ) -> None:
        cache_key = self.key(instruction, app, state_hash)
        async with aiosqlite.connect(self.path) as db:
            await db.execute(
                """
                INSERT INTO workflow_cache (
                    cache_key, app, state_hash, instruction_hash, plan_json,
                    success_count, failure_count, avg_latency_ms
                ) VALUES (?, ?, ?, ?, ?, 0, 1, 0)
                ON CONFLICT(cache_key) DO UPDATE SET
                    failure_count = workflow_cache.failure_count + 1,
                    updated_at = CURRENT_TIMESTAMP
                """,
                (
                    cache_key,
                    app,
                    state_hash,
                    self.instruction_hash(instruction),
                    plan.model_dump_json(),
                ),
            )
            await db.commit()

    async def stats(self) -> dict[str, Any]:
        async with aiosqlite.connect(self.path) as db:
            row = await (
                await db.execute(
                    """SELECT COUNT(*), COALESCE(SUM(success_count), 0),
                    COALESCE(SUM(failure_count), 0) FROM workflow_cache"""
                )
            ).fetchone()
        count, successes, failures = row or (0, 0, 0)
        return {"entries": count, "successes": successes, "failures": failures}
