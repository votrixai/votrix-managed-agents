import pytest

from app.config import get_settings
from app.storage import (
    StorageConfigurationError,
    is_object_storage_backend,
    object_storage_backend_label,
    object_storage_configured,
    object_key,
    public_url_for_key,
    should_store_in_object_storage,
)


def _clear_s3_env(monkeypatch):
    for key in (
        "S3_ENDPOINT_URL",
        "S3_ACCESS_KEY_ID",
        "S3_SECRET_ACCESS_KEY",
        "S3_BUCKET_NAME",
        "S3_PUBLIC_URL",
        "S3_REGION",
    ):
        monkeypatch.delenv(key, raising=False)


def test_s3_object_storage_configuration(monkeypatch):
    monkeypatch.setenv("S3_ENDPOINT_URL", "https://storage.example.com")
    monkeypatch.setenv("S3_ACCESS_KEY_ID", "key")
    monkeypatch.setenv("S3_SECRET_ACCESS_KEY", "secret")
    monkeypatch.setenv("S3_BUCKET_NAME", "vma-files")
    monkeypatch.setenv("S3_PUBLIC_URL", "https://cdn.example.com/vma-files")
    monkeypatch.setenv("S3_REGION", "us-east-1")
    get_settings.cache_clear()

    assert object_storage_configured() is True
    assert should_store_in_object_storage() is True
    assert object_storage_backend_label() == "s3"
    assert public_url_for_key("agents/file.txt") == "https://cdn.example.com/vma-files/agents/file.txt"
    assert is_object_storage_backend("s3") is True
    assert object_key(
        namespace="vma",
        category="files",
        filename="file.txt",
        content_sha256="abcdef1234567890",
        workspace_id="ws_test",
    ).startswith("workspaces/ws_test/vma/files/")


def test_object_storage_requires_s3_configuration(monkeypatch):
    _clear_s3_env(monkeypatch)
    get_settings.cache_clear()

    with pytest.raises(StorageConfigurationError):
        should_store_in_object_storage()
