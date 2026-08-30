from __future__ import annotations

import sqlite3
from types import SimpleNamespace
from unittest.mock import AsyncMock

from sqlalchemy.exc import IntegrityError

from app.services import files as service
from app.utils.storage import StoredObject


async def test_database_unique_index_resolves_a_cross_instance_file_race(
    monkeypatch,
):
    original = sqlite3.IntegrityError(
        "UNIQUE constraint failed: files.organization_id, files.idempotency_key"
    )
    collision = IntegrityError("INSERT INTO files", {}, original)
    winner = SimpleNamespace(storage_key="winner-key")
    db = AsyncMock()
    stored = StoredObject(
        key="loser-key",
        content_type="text/plain",
        size_bytes=6,
        sha256="a" * 64,
    )

    monkeypatch.setattr(service.storage, "save_bytes", AsyncMock(return_value=stored))
    monkeypatch.setattr(service.storage, "delete_object", AsyncMock())
    monkeypatch.setattr(
        service.files_q,
        "get_by_idempotency_key",
        AsyncMock(side_effect=[None, winner]),
    )
    monkeypatch.setattr(
        service.files_q,
        "create_file",
        AsyncMock(side_effect=collision),
    )

    result = await service.upload_file(
        db,
        organization_id="org_test",
        filename="legacy.txt",
        mime_type="text/plain",
        content=b"legacy",
        idempotency_key="cma-snapshot/file",
    )

    assert result is winner
    db.rollback.assert_awaited_once()
    service.storage.delete_object.assert_awaited_once_with("loser-key")


async def test_file_race_does_not_delete_a_content_addressed_winner(monkeypatch):
    original = sqlite3.IntegrityError(
        "UNIQUE constraint failed: files.organization_id, files.idempotency_key"
    )
    collision = IntegrityError("INSERT INTO files", {}, original)
    winner = SimpleNamespace(storage_key="shared-key")
    db = AsyncMock()
    stored = StoredObject(
        key="shared-key",
        content_type="text/plain",
        size_bytes=6,
        sha256="a" * 64,
    )

    monkeypatch.setattr(service.storage, "save_bytes", AsyncMock(return_value=stored))
    monkeypatch.setattr(service.storage, "delete_object", AsyncMock())
    monkeypatch.setattr(
        service.files_q,
        "get_by_idempotency_key",
        AsyncMock(side_effect=[None, winner]),
    )
    monkeypatch.setattr(
        service.files_q,
        "create_file",
        AsyncMock(side_effect=collision),
    )

    result = await service.upload_file(
        db,
        organization_id="org_test",
        filename="legacy.txt",
        mime_type="text/plain",
        content=b"legacy",
        idempotency_key="cma-snapshot/file",
    )

    assert result is winner
    service.storage.delete_object.assert_not_awaited()
