from datetime import datetime, timedelta, timezone

import pytest

from sentri.config import Settings
from sentri.models import TelemetryEvent, WorkerName
from sentri.storage import SentriStorageEngine


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["ephemeral", "sqlite", "jsonl"])
async def test_storage_modes_round_trip_and_redact(tmp_path, mode: str) -> None:
    settings = Settings(storage_mode=mode, data_dir=tmp_path, retention_days=30)
    storage = SentriStorageEngine(settings)
    await storage.initialize()
    try:
        event = TelemetryEvent(
            execution_id="execution-1",
            worker=WorkerName.ROGUE,
            kind="test",
            payload={"email": "person@example.com"},
        )
        await storage.record(event)
        found = await storage.query("execution-1")
        assert len(found) == 1
        assert "person@example.com" not in found[0].payload["email"]
        assert "HASHED_EMAIL" in found[0].payload["email"]
    finally:
        await storage.close()
