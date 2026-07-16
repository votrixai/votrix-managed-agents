import pytest

from app.config import get_settings
from app.storage import (
    StorageConfigurationError,
    is_object_storage_backend,
    object_storage_backend_label,
    object_storage_configured,
    object_key,
    should_store_in_object_storage,
)


def _clear_s3_env(monkeypatch):
    for key in (
        "S3_ENDPOINT_URL",
        "S3_ACCESS_KEY_ID",
        "S3_SECRET_ACCESS_KEY",
        "S3_BUCKET_NAME",
        "S3_REGION",
    ):
        # An explicit empty value overrides credentials from a developer .env.
        monkeypatch.setenv(key, "")


def test_s3_object_storage_configuration(monkeypatch):
    monkeypatch.setenv("S3_ENDPOINT_URL", "https://storage.example.com")
    monkeypatch.setenv("S3_ACCESS_KEY_ID", "key")
    monkeypatch.setenv("S3_SECRET_ACCESS_KEY", "secret")
    monkeypatch.setenv("S3_BUCKET_NAME", "vma-files")
    monkeypatch.setenv("S3_REGION", "us-east-1")
    get_settings.cache_clear()

    assert object_storage_configured() is True
    assert should_store_in_object_storage() is True
    assert object_storage_backend_label() == "s3"
    assert is_object_storage_backend("s3") is True
    assert object_key(
        namespace="vma",
        category="files",
        filename="file.txt",
        content_sha256="abcdef1234567890",
        organization_id="org_test",
    ).startswith("organizations/org_test/vma/files/")


def test_object_storage_requires_s3_configuration(monkeypatch):
    _clear_s3_env(monkeypatch)
    get_settings.cache_clear()

    with pytest.raises(StorageConfigurationError):
        should_store_in_object_storage()


@pytest.mark.parametrize(
    "organization_id",
    ["default", "org_", "org_../escape", "org_" + "default"],
)
def test_object_key_requires_an_explicit_prefixed_organization_id(organization_id):
    with pytest.raises(TypeError):
        object_key(namespace="vma", category="files", filename="file.txt")

    with pytest.raises(ValueError, match="organization_id"):
        object_key(
            namespace="vma",
            category="files",
            filename="file.txt",
            organization_id=organization_id,
        )
