import base64
import json
import sys
from pathlib import Path

import httpx
import pytest

from app.config import get_settings
from app.db.engine import session_scope
from app.db.models import Organization
from app.factory import create_app
from app.organization import CurrentOrganization


SDK_SOURCE = Path(__file__).resolve().parents[2] / "sdks" / "python" / "src"
if str(SDK_SOURCE) not in sys.path:
    sys.path.insert(0, str(SDK_SOURCE))

from votrix import AsyncVotrix, ConflictError  # noqa: E402


class _SDKContractAuthProvider:
    async def authenticate(self, request, credentials):
        return CurrentOrganization(id="org_sdk_contract", slug="sdk-contract", source="test")


@pytest.fixture
async def sdk(monkeypatch):
    monkeypatch.setenv("VMA_ENCRYPTION_KEY", base64.b64encode(b"s" * 32).decode())
    monkeypatch.setenv(
        "VMA_MODEL_PROVIDERS",
        json.dumps(
            {
                "openrouter": {
                    "display_name": "OpenRouter Fast",
                    "adapter": "openrouter",
                    "api_key_env": "OPENROUTER_API_KEY",
                    "default_model": "deepseek/deepseek-v4-pro",
                }
            }
        ),
    )
    get_settings.cache_clear()

    async with session_scope() as db:
        db.add(
            Organization(
                id="org_sdk_contract",
                slug="sdk-contract",
                name="SDK contract organization",
                metadata_={},
            )
        )
        await db.commit()

    app = create_app(auth_provider=_SDKContractAuthProvider())
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://sdk-contract.test",
    ) as http_client:
        async with AsyncVotrix(
            api_key="vma_sdk_contract_key",
            base_url="http://sdk-contract.test",
            http_client=http_client,
        ) as client:
            yield client


async def test_native_sdk_provider_byok_and_session_binding_are_secret_free(sdk):
    providers = await sdk.model_providers.list()
    provider = next(item for item in providers.data if item.id == "openrouter")
    assert provider.display_name == "OpenRouter Fast"
    assert provider.credential_type == "api_key"
    assert "api_key_env" not in provider.model_extra

    vault = await sdk.vaults.create(display_name="End-user model key")
    end_user_key = "sk-sdk-contract-secret"
    credential = await sdk.vaults.model_credentials.create(
        vault.id,
        provider=provider.id,
        api_key=end_user_key,
        display_name="End-user OpenRouter",
    )
    assert credential.vault_id == vault.id
    assert credential.model_provider == "openrouter"
    assert "auth" not in (credential.model_extra or {})
    assert "secret_name" not in (credential.model_extra or {})
    assert end_user_key not in credential.model_dump_json()

    with pytest.raises(ConflictError) as duplicate:
        await sdk.vaults.model_credentials.create(
            vault.id,
            provider=provider.id,
            api_key="sk-must-not-appear",
        )
    assert duplicate.value.status_code == 409
    assert duplicate.value.error_code == "resource_conflict"
    assert "openrouter" in str(duplicate.value).lower()
    assert "sk-must-not-appear" not in repr(duplicate.value)

    agent = await sdk.agents.create(
        name="SDK contract agent",
        model={"id": "deepseek/deepseek-v4-pro", "provider": provider.id},
    )
    environment = await sdk.environments.create(
        name="sdk-contract-environment",
        config={"type": "cloud"},
    )
    session = await sdk.sessions.create(
        agent=agent.id,
        environment_id=environment.id,
        vault_ids=[vault.id],
    )

    binding = session.status_details["model_credential_binding"]
    assert binding == {
        "version": 1,
        "source": "vault",
        "credential_id": credential.id,
        "vault_id": vault.id,
        "model_provider": "openrouter",
    }
    serialized = session.model_dump_json()
    assert "OPENROUTER_API_KEY" not in serialized
    assert "secret_name" not in serialized
    assert end_user_key not in serialized

    rotated_key = "sk-sdk-contract-rotated"
    rotated = await sdk.vaults.model_credentials.rotate(
        vault.id,
        credential.id,
        api_key=rotated_key,
    )
    assert rotated.id == credential.id
    assert rotated.model_provider == "openrouter"
    rotated_json = rotated.model_dump_json()
    assert "OPENROUTER_API_KEY" not in rotated_json
    assert "secret_name" not in rotated_json
    assert rotated_key not in rotated_json

    credential_page = await sdk.vaults.model_credentials.list(vault.id)
    assert [item.id for item in credential_page.data] == [credential.id]
    retrieved = await sdk.vaults.model_credentials.retrieve(
        credential.id,
        vault_id=vault.id,
    )
    assert retrieved.id == credential.id
    assert "secret_name" not in retrieved.model_dump_json()

    archived = await sdk.vaults.model_credentials.archive(
        credential.id,
        vault_id=vault.id,
    )
    assert archived.archived_at is not None
    assert "secret_name" not in archived.model_dump_json()
    active_page = await sdk.vaults.model_credentials.list(vault.id)
    assert active_page.data == []
    archived_page = await sdk.vaults.model_credentials.list(
        vault.id,
        include_archived=True,
    )
    assert [item.id for item in archived_page.data] == [credential.id]

    replacement = await sdk.vaults.model_credentials.create(
        vault.id,
        provider=provider.id,
        api_key="sk-sdk-contract-delete",
    )
    deleted = await sdk.vaults.model_credentials.delete(
        replacement.id,
        vault_id=vault.id,
    )
    assert deleted.id == replacement.id
    assert deleted.type == "model_credential_deleted"
    assert "sk-sdk-contract-delete" not in deleted.model_dump_json()

    first_page = await sdk.agents.list(limit=1)
    assert first_page.data[0].id == agent.id


async def test_native_sdk_files_skills_and_binary_download(sdk, tmp_path):
    uploaded = await sdk.files.upload(
        file=b"SDK binary contract",
        filename="contract.txt",
        mime_type="text/plain",
    )
    assert uploaded.filename == "contract.txt"

    download = await sdk.files.download(uploaded.id)
    assert await download.read() == b"SDK binary contract"
    assert b"".join(download.iter_bytes(chunk_size=4)) == b"SDK binary contract"
    destination = await download.write_to_file(tmp_path / "downloaded.txt")
    assert destination.read_bytes() == b"SDK binary contract"

    streamed_download = await sdk.files.download(uploaded.id, stream=True)
    streamed = b"".join(
        [chunk async for chunk in streamed_download.aiter_bytes(chunk_size=4)]
    )
    assert streamed == b"SDK binary contract"

    skill = await sdk.skills.create(
        display_title="SDK test skill",
        files=[
            {
                "filename": "sdk-test-skill/SKILL.md",
                "mime_type": "text/markdown",
                "content": (
                    "---\n"
                    "name: sdk-test-skill\n"
                    "description: Exercises the native SDK contract.\n"
                    "---\n\n"
                    "Use this skill in SDK contract tests.\n"
                ),
            }
        ],
    )
    assert skill.display_title == "SDK test skill"
    assert skill.version is not None

    assert not hasattr(sdk, "memory_stores")
    assert not hasattr(sdk.vaults, "credentials")


async def test_native_sdk_api_key_lifecycle_returns_plaintext_only_once(sdk):
    created = await sdk.api_keys.create(
        name="SDK contract key",
        scopes=["api", "api_keys:manage"],
        metadata={"test": "sdk-contract"},
    )
    assert created.secret.get_secret_value().startswith("vma_")

    listed = await sdk.api_keys.list(include_revoked=False)
    safe = next(item for item in listed.data if item.id == created.id)
    retrieved = await sdk.api_keys.retrieve(created.id)
    assert "secret" not in safe.model_dump()
    assert "secret" not in retrieved.model_dump()

    rotated = await sdk.api_keys.rotate(created.id, reason="SDK contract rollover")
    assert rotated.secret.get_secret_value().startswith("vma_")
    assert rotated.replaces_key_id == created.id

    revoked = await sdk.api_keys.revoke(rotated.id, reason="SDK contract complete")
    assert revoked.revoked_at is not None
    assert "secret" not in revoked.model_dump()
