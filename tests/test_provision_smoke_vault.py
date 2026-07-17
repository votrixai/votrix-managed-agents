from __future__ import annotations

from types import SimpleNamespace

import pytest

from scripts.provision_smoke_vault import PROVISIONER, provision_smoke_vault


class _AsyncItems:
    def __init__(self, items):
        self.items = list(items)

    def __aiter__(self):
        async def iterate():
            for item in self.items:
                yield item

        return iterate()


class _ModelCredentials:
    def __init__(self, items=()):
        self.items = list(items)
        self.create_calls = []

    def list(self, _vault_id):
        return _AsyncItems(self.items)

    async def create(self, vault_id, **body):
        self.create_calls.append((vault_id, body))
        item = SimpleNamespace(
            id="cred_new",
            model_provider=body["provider"],
        )
        self.items.append(item)
        return item


class _Vaults:
    def __init__(self, items=(), credentials=()):
        self.items = list(items)
        self.create_calls = []
        self.model_credentials = _ModelCredentials(credentials)

    def list(self):
        return _AsyncItems(self.items)

    async def create(self, **body):
        self.create_calls.append(body)
        item = SimpleNamespace(id="vault_new", metadata=body["metadata"])
        self.items.append(item)
        return item


def _client(*, vaults=(), credentials=()):
    return SimpleNamespace(
        model_providers=SimpleNamespace(
            list=lambda: _AsyncItems([SimpleNamespace(id="openrouter")])
        ),
        vaults=_Vaults(vaults, credentials),
    )


async def test_provisions_vault_and_write_only_model_credential():
    client = _client()

    result = await provision_smoke_vault(
        client,
        provider="openrouter",
        model_api_key="secret-model-key",
        display_name="Acceptance",
    )

    assert result.vault_id == "vault_new"
    assert result.credential_id == "cred_new"
    assert result.vault_created is True
    assert result.credential_created is True
    assert client.vaults.create_calls[0]["metadata"]["provisioned_by"] == PROVISIONER
    assert client.vaults.model_credentials.create_calls[0][1]["api_key"] == "secret-model-key"


async def test_reuses_existing_provisioned_vault_and_credential():
    vault = SimpleNamespace(id="vault_existing", metadata={"provisioned_by": PROVISIONER})
    credential = SimpleNamespace(id="cred_existing", model_provider="openrouter")
    client = _client(vaults=[vault], credentials=[credential])

    result = await provision_smoke_vault(
        client,
        provider="openrouter",
        model_api_key="unused-on-retry",
        display_name="Acceptance",
    )

    assert result.vault_id == "vault_existing"
    assert result.credential_id == "cred_existing"
    assert result.vault_created is False
    assert result.credential_created is False
    assert client.vaults.create_calls == []
    assert client.vaults.model_credentials.create_calls == []


async def test_rejects_provider_not_enabled_by_server():
    with pytest.raises(ValueError, match="not enabled"):
        await provision_smoke_vault(
            _client(),
            provider="anthropic",
            model_api_key="secret",
            display_name="Acceptance",
        )
