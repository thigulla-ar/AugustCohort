from __future__ import annotations

import asyncio
import json
import sqlite3
import secrets
from abc import ABC, abstractmethod
from collections import deque
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import aiosqlite

from sentri.config import Settings, StorageMode
from sentri.models import TelemetryEvent
from sentri.redaction import redact
from sentri.security import sign_telemetry_event


class StorageBackend(ABC):
    @abstractmethod
    async def initialize(self) -> None: ...

    @abstractmethod
    async def append(self, event: TelemetryEvent) -> None: ...

    @abstractmethod
    async def query(
        self, execution_id: str | None = None, limit: int = 500
    ) -> list[dict[str, Any]]: ...

    @abstractmethod
    async def purge_before(self, cutoff: datetime) -> int: ...

    async def close(self) -> None:
        return None


class EphemeralBackend(StorageBackend):
    def __init__(self) -> None:
        self.events: deque[dict[str, Any]] = deque()
        self.lock = asyncio.Lock()

    async def initialize(self) -> None:
        return None

    async def append(self, event: TelemetryEvent) -> None:
        async with self.lock:
            self.events.append(event.model_dump(mode="json"))

    async def query(
        self, execution_id: str | None = None, limit: int = 500
    ) -> list[dict[str, Any]]:
        async with self.lock:
            matches = [
                event
                for event in self.events
                if execution_id is None or event["execution_id"] == execution_id
            ]
        return matches[-limit:]

    async def purge_before(self, cutoff: datetime) -> int:
        removed = 0
        async with self.lock:
            retained: deque[dict[str, Any]] = deque()
            for event in self.events:
                timestamp = datetime.fromisoformat(event["timestamp"])
                if timestamp < cutoff:
                    removed += 1
                else:
                    retained.append(event)
            self.events = retained
        return removed


class SQLiteBackend(StorageBackend):
    def __init__(self, path: Path) -> None:
        self.path = path
        self.lock = asyncio.Lock()

    async def initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        async with aiosqlite.connect(self.path) as db:
            await db.executescript(
                """
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS telemetry (
                    event_id TEXT PRIMARY KEY,
                    execution_id TEXT NOT NULL,
                    worker TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    timestamp TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    sequence INTEGER,
                    previous_hash TEXT,
                    integrity_hash TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_telemetry_execution_time
                    ON telemetry(execution_id, timestamp);
                CREATE INDEX IF NOT EXISTS idx_telemetry_worker_time
                    ON telemetry(worker, timestamp);
                CREATE INDEX IF NOT EXISTS idx_telemetry_timestamp
                    ON telemetry(timestamp);
                """
            )
            columns = {
                row[1] for row in await (await db.execute("PRAGMA table_info(telemetry)" )).fetchall()
            }
            for name, column_type in (
                ("sequence", "INTEGER"),
                ("previous_hash", "TEXT"),
                ("integrity_hash", "TEXT"),
            ):
                if name not in columns:
                    await db.execute(f"ALTER TABLE telemetry ADD COLUMN {name} {column_type}")
            await db.commit()

    async def append(self, event: TelemetryEvent) -> None:
        record = event.model_dump(mode="json")
        async with self.lock:
            async with aiosqlite.connect(self.path) as db:
                await db.execute(
                    """
                    INSERT INTO telemetry
                    (event_id, execution_id, worker, kind, timestamp, payload_json,
                     sequence, previous_hash, integrity_hash)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        record["event_id"],
                        record["execution_id"],
                        record["worker"],
                        record["kind"],
                        record["timestamp"],
                        json.dumps(record["payload"], separators=(",", ":")),
                        record["sequence"],
                        record["previous_hash"],
                        record["integrity_hash"],
                    ),
                )
                await db.commit()

    async def query(
        self, execution_id: str | None = None, limit: int = 500
    ) -> list[dict[str, Any]]:
        clause = "WHERE execution_id = ?" if execution_id else ""
        params: tuple[Any, ...] = (execution_id, limit) if execution_id else (limit,)
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = sqlite3.Row
            cursor = await db.execute(
                f"""
                SELECT event_id, execution_id, worker, kind, timestamp, payload_json,
                       sequence, previous_hash, integrity_hash
                FROM telemetry {clause}
                ORDER BY timestamp DESC LIMIT ?
                """,
                params,
            )
            rows = await cursor.fetchall()
        return [
            {
                "event_id": row["event_id"],
                "execution_id": row["execution_id"],
                "worker": row["worker"],
                "kind": row["kind"],
                "timestamp": row["timestamp"],
                "payload": json.loads(row["payload_json"]),
                "sequence": row["sequence"],
                "previous_hash": row["previous_hash"],
                "integrity_hash": row["integrity_hash"],
            }
            for row in reversed(rows)
        ]

    async def purge_before(self, cutoff: datetime) -> int:
        async with self.lock:
            async with aiosqlite.connect(self.path) as db:
                cursor = await db.execute(
                    "DELETE FROM telemetry WHERE timestamp < ?", (cutoff.isoformat(),)
                )
                await db.commit()
                return max(cursor.rowcount, 0)


class JSONLBackend(StorageBackend):
    def __init__(self, directory: Path) -> None:
        self.directory = directory
        self.lock = asyncio.Lock()

    async def initialize(self) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)

    def _today_path(self) -> Path:
        today = datetime.now(timezone.utc).date().isoformat()
        return self.directory / f"{today}_sentri.jsonl"

    async def append(self, event: TelemetryEvent) -> None:
        line = event.model_dump_json() + "\n"
        async with self.lock:
            await asyncio.to_thread(self._append_sync, self._today_path(), line)

    @staticmethod
    def _append_sync(path: Path, line: str) -> None:
        with path.open("a", encoding="utf-8") as stream:
            stream.write(line)
            stream.flush()

    async def query(
        self, execution_id: str | None = None, limit: int = 500
    ) -> list[dict[str, Any]]:
        return await asyncio.to_thread(self._query_sync, execution_id, limit)

    def _query_sync(
        self, execution_id: str | None, limit: int
    ) -> list[dict[str, Any]]:
        matches: deque[dict[str, Any]] = deque(maxlen=limit)
        for path in sorted(self.directory.glob("*_sentri.jsonl")):
            with path.open("r", encoding="utf-8") as stream:
                for line in stream:
                    try:
                        event = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if execution_id is None or event.get("execution_id") == execution_id:
                        matches.append(event)
        return list(matches)

    async def purge_before(self, cutoff: datetime) -> int:
        return await asyncio.to_thread(self._purge_sync, cutoff)

    def _purge_sync(self, cutoff: datetime) -> int:
        removed = 0
        for path in self.directory.glob("*_sentri.jsonl"):
            try:
                file_date = datetime.strptime(
                    path.name[:10], "%Y-%m-%d"
                ).replace(tzinfo=timezone.utc)
            except ValueError:
                continue
            if file_date < cutoff.replace(hour=0, minute=0, second=0, microsecond=0):
                path.unlink()
                removed += 1
        return removed


class SentriStorageEngine:
    """Mode-selectable local telemetry store with redaction and retention."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._integrity_secret = settings.signing_secret or secrets.token_urlsafe(48)
        self.backend = self._create_backend(settings.storage_mode)
        self._backend_lock = asyncio.Lock()
        self._retention_task: asyncio.Task[None] | None = None
        self._subscribers: set[asyncio.Queue[dict[str, Any]]] = set()

    def _create_backend(self, mode: StorageMode) -> StorageBackend:
        if mode == "ephemeral":
            return EphemeralBackend()
        if mode == "jsonl":
            return JSONLBackend(self.settings.data_dir / "logs")
        return SQLiteBackend(self.settings.data_dir / "sentri.db")

    async def initialize(self) -> None:
        await self.backend.initialize()
        await self.purge_expired()
        self._retention_task = asyncio.create_task(
            self._retention_loop(), name="sentri-retention"
        )

    async def close(self) -> None:
        if self._retention_task:
            self._retention_task.cancel()
            try:
                await self._retention_task
            except asyncio.CancelledError:
                pass
        await self.backend.close()

    async def record(self, event: TelemetryEvent) -> None:
        safe = event.model_copy(update={"payload": redact(event.payload)})
        async with self._backend_lock:
            previous_events = await self.backend.query(limit=1)
            previous = (
                TelemetryEvent.model_validate(previous_events[-1])
                if previous_events
                else None
            )
            safe = safe.model_copy(
                update={
                    "sequence": (previous.sequence or 0) + 1 if previous else 1,
                    "previous_hash": previous.integrity_hash if previous else None,
                }
            )
            safe = safe.model_copy(
                update={"integrity_hash": sign_telemetry_event(safe, self._integrity_secret)}
            )
            await self.backend.append(safe)
        serialized = safe.model_dump(mode="json")
        for queue in tuple(self._subscribers):
            try:
                queue.put_nowait(serialized)
            except asyncio.QueueFull:
                try:
                    queue.get_nowait()
                    queue.put_nowait(serialized)
                except (asyncio.QueueEmpty, asyncio.QueueFull):
                    pass

    async def record_many(self, events: list[TelemetryEvent]) -> None:
        for event in events:
            await self.record(event)

    async def query(
        self, execution_id: str | None = None, limit: int = 500
    ) -> list[TelemetryEvent]:
        async with self._backend_lock:
            rows = await self.backend.query(
                execution_id, min(max(limit, 1), 5_000)
            )
        return [TelemetryEvent.model_validate(row) for row in rows]

    async def purge_expired(self) -> int:
        if self.settings.retention_days is None:
            return 0
        cutoff = datetime.now(timezone.utc) - timedelta(
            days=self.settings.retention_days
        )
        async with self._backend_lock:
            return await self.backend.purge_before(cutoff)

    async def verify_integrity(
        self, execution_id: str | None = None, limit: int = 5_000
    ) -> dict[str, Any]:
        events = await self.query(execution_id, limit)
        errors: list[str] = []
        previous: TelemetryEvent | None = None
        for event in events:
            if not event.integrity_hash:
                errors.append(f"{event.event_id}: legacy event has no integrity hash")
            else:
                expected = sign_telemetry_event(event, self._integrity_secret)
                if expected != event.integrity_hash:
                    errors.append(f"{event.event_id}: integrity hash mismatch")
            if previous and event.sequence == (previous.sequence or 0) + 1:
                if event.previous_hash != previous.integrity_hash:
                    errors.append(f"{event.event_id}: previous hash mismatch")
            previous = event
        return {
            "valid": not errors,
            "checked_events": len(events),
            "errors": errors,
        }

    async def reconfigure(
        self, storage_mode: StorageMode, retention_days: int | None
    ) -> int:
        """Switch active storage and apply retention without deleting other stores."""
        if storage_mode != self.settings.storage_mode:
            replacement = self._create_backend(storage_mode)
            await replacement.initialize()
            async with self._backend_lock:
                previous = self.backend
                self.backend = replacement
                self.settings.storage_mode = storage_mode
            await previous.close()
        self.settings.retention_days = retention_days
        return await self.purge_expired()

    def active_path(self) -> str:
        if self.settings.storage_mode == "ephemeral":
            return "RAM only"
        if self.settings.storage_mode == "jsonl":
            return str(self.settings.data_dir / "logs")
        return str(self.settings.data_dir / "sentri.db")

    async def _retention_loop(self) -> None:
        while True:
            await asyncio.sleep(self.settings.retention_interval_seconds)
            await self.purge_expired()

    def subscribe(self) -> asyncio.Queue[dict[str, Any]]:
        if len(self._subscribers) >= self.settings.max_dashboard_subscribers:
            raise RuntimeError("Maximum dashboard subscriber count reached.")
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=500)
        self._subscribers.add(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue[dict[str, Any]]) -> None:
        self._subscribers.discard(queue)
