from __future__ import annotations

import base64
from datetime import datetime, timedelta, timezone

import pytest

from app.config import get_settings
from app.db.engine import session_scope
from app.db.queries import agents as agents_q
from app.db.queries import organization_funding as organization_funding_q
from app.db.queries import session_funding_bindings as funding_bindings_q
from app.db.queries import sessions as sessions_q
from app.runtime.agent_resolution import effective_agent_version
from app.runtime.credential_broker import session_credential_broker
from app.runtime.model_credentials import ModelCredentialUnavailableError
from tests.conftest import TEST_HEADERS


_FUNDING_NOT_GIVEN = object()
_TEST_ENCRYPTION_KEY = base64.b64encode(b"p" * 32).decode()


@pytest.fixture(autouse=True)
def platform_funding_encryption_key(monkeypatch):
    monkeypatch.setenv("VMA_ENCRYPTION_KEY", _TEST_ENCRYPTION_KEY)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


async def _create_agent(client, *, headers=TEST_HEADERS):
    response = await client.post(
        "/v1/agents",
        headers=headers,
        json={
            "name": "Platform funding agent",
            "model": {
                "id": "deepseek/deepseek-v4-pro",
                "provider": "openrouter",
            },
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


async def _create_environment(client, *, headers=TEST_HEADERS):
    response = await client.post(
        "/v1/environments",
        headers=headers,
        json={"name": "platform-funding", "config": {"type": "cloud"}},
    )
    assert response.status_code == 201, response.text
    return response.json()


async def _create_vault_credential(
    client,
    *,
    api_key: str,
    headers=TEST_HEADERS,
) -> str:
    response = await client.post(
        "/v1/vaults",
        headers=headers,
        json={"display_name": "Organization BYOK"},
    )
    assert response.status_code == 201, response.text
    vault_id = response.json()["id"]
    response = await client.post(
        f"/v1/vaults/{vault_id}/credentials",
        headers=headers,
        json={
            "display_name": "Organization OpenRouter",
            "auth": {
                "type": "environment_variable",
                "secret_name": "OPENROUTER_API_KEY",
                "secret_value": api_key,
                "networking": {"type": "unrestricted"},
            },
        },
    )
    assert response.status_code == 201, response.text
    return vault_id


async def _provision_platform_funding(
    *,
    policy: str,
    api_key: str,
    organization_id: str = "org_test",
) -> tuple[str, str]:
    async with session_scope() as db:
        account = await organization_funding_q.create_organization_billing_account(
            db,
            policy=policy,
            trial_expires_at=datetime.now(timezone.utc) + timedelta(days=7),
            organization_id=organization_id,
        )
        provider_key = (
            await organization_funding_q.create_organization_provider_key_binding(
                db,
                organization_billing_account_id=account.id,
                provider="openrouter",
                api_key=api_key,
                spending_limit_usd_micros=5_000_000,
                organization_id=organization_id,
            )
        )
        account_id = account.id
        provider_key_id = provider_key.id
        await db.commit()
    return account_id, provider_key_id


async def _create_session(
    client,
    *,
    agent_id: str,
    environment_id: str,
    vault_ids: list[str] | None = None,
    funding_type: str | object = _FUNDING_NOT_GIVEN,
    headers=TEST_HEADERS,
):
    body = {
        "agent": agent_id,
        "environment_id": environment_id,
        "vault_ids": list(vault_ids or []),
    }
    if funding_type is not _FUNDING_NOT_GIVEN:
        body["funding"] = {"type": funding_type}
    return await client.post("/v1/sessions", headers=headers, json=body)


async def _provider_secrets(
    session_id: str,
    *,
    organization_id: str = "org_test",
) -> dict[str, str]:
    async with session_scope() as db:
        session = await sessions_q.get_session(
            db,
            session_id,
            organization_id=organization_id,
        )
        assert session is not None
        version = await agents_q.get_agent_version(
            db,
            agent_id=session.agent_id,
            version=session.agent_version,
            organization_id=organization_id,
        )
        assert version is not None
        return await session_credential_broker.resolve_provider_secrets(
            db,
            session=session,
            version=effective_agent_version(version, session.status_details),
        )


async def _durable_binding(
    session_id: str,
    *,
    organization_id: str = "org_test",
):
    async with session_scope() as db:
        binding = await funding_bindings_q.get_session_funding_binding(
            db,
            session_id,
            organization_id=organization_id,
        )
        assert binding is not None
        return {
            "source": binding.source,
            "vault_id": binding.vault_id,
            "model_credential_id": binding.model_credential_id,
            "organization_billing_account_id": (
                binding.organization_billing_account_id
            ),
            "organization_provider_key_binding_id": (
                binding.organization_provider_key_binding_id
            ),
        }


async def test_omitted_funding_preserves_byok_compatibility_without_billing_account(
    client,
):
    agent = await _create_agent(client)
    environment = await _create_environment(client)
    vault_id = await _create_vault_credential(client, api_key="byok-compatible")

    response = await _create_session(
        client,
        agent_id=agent["id"],
        environment_id=environment["id"],
        vault_ids=[vault_id],
    )

    assert response.status_code == 201, response.text
    session = response.json()
    assert session["status_details"]["model_credential_binding"]["source"] == "vault"
    assert await _provider_secrets(session["id"]) == {
        "OPENROUTER_API_KEY": "byok-compatible"
    }


@pytest.mark.parametrize(
    ("policy", "funding_type", "expected_status", "expected_source"),
    (
        ("byok_only", _FUNDING_NOT_GIVEN, 201, "vault"),
        ("platform_only", _FUNDING_NOT_GIVEN, 201, "platform"),
        ("prefer_byok", "organization_default", 201, "vault"),
        ("prefer_platform", "organization_default", 201, "platform"),
        ("prefer_platform", "byok", 201, "vault"),
        ("prefer_byok", "platform_credits", 201, "platform"),
        ("byok_only", "platform_credits", 422, None),
        ("platform_only", "byok", 422, None),
    ),
)
async def test_session_funding_policy_matrix(
    client,
    policy,
    funding_type,
    expected_status,
    expected_source,
):
    await _provision_platform_funding(
        policy=policy,
        api_key=f"platform-{policy}",
    )
    agent = await _create_agent(client)
    environment = await _create_environment(client)
    vault_id = await _create_vault_credential(
        client,
        api_key=f"byok-{policy}",
    )

    response = await _create_session(
        client,
        agent_id=agent["id"],
        environment_id=environment["id"],
        vault_ids=[vault_id],
        funding_type=funding_type,
    )

    assert response.status_code == expected_status, response.text
    if expected_status == 422:
        assert response.json()["error"]["code"] == "session_funding_unavailable"
        return
    assert (
        response.json()["status_details"]["model_credential_binding"]["source"]
        == expected_source
    )


@pytest.mark.parametrize(
    ("policy", "available_source", "expected_source"),
    (
        ("prefer_byok", "platform", "platform"),
        ("prefer_platform", "vault", "vault"),
    ),
)
async def test_preference_policy_falls_back_only_while_creating_session(
    client,
    policy,
    available_source,
    expected_source,
):
    async with session_scope() as db:
        account = await organization_funding_q.create_organization_billing_account(
            db,
            policy=policy,
            trial_expires_at=datetime.now(timezone.utc) + timedelta(days=7),
        )
        if available_source == "platform":
            await organization_funding_q.create_organization_provider_key_binding(
                db,
                organization_billing_account_id=account.id,
                provider="openrouter",
                api_key="only-platform-is-available",
            )
        await db.commit()

    agent = await _create_agent(client)
    environment = await _create_environment(client)
    vault_ids = []
    if available_source == "vault":
        vault_ids = [
            await _create_vault_credential(
                client,
                api_key="only-byok-is-available",
            )
        ]
    response = await _create_session(
        client,
        agent_id=agent["id"],
        environment_id=environment["id"],
        vault_ids=vault_ids,
        funding_type="organization_default",
    )

    assert response.status_code == 201, response.text
    assert (
        response.json()["status_details"]["model_credential_binding"]["source"]
        == expected_source
    )


async def test_platform_response_excludes_secret_and_private_coordinates_and_broker_uses_exact_key(
    client,
):
    platform_key = "platform-secret-never-public"
    account_id, provider_key_id = await _provision_platform_funding(
        policy="platform_only",
        api_key=platform_key,
    )
    agent = await _create_agent(client)
    environment = await _create_environment(client)

    response = await _create_session(
        client,
        agent_id=agent["id"],
        environment_id=environment["id"],
        funding_type="platform_credits",
    )

    assert response.status_code == 201, response.text
    session = response.json()
    public_binding = session["status_details"]["model_credential_binding"]
    assert public_binding == {
        "version": 1,
        "source": "platform",
        "credential_id": None,
        "vault_id": None,
        "model_provider": "openrouter",
    }
    assert platform_key not in response.text
    assert account_id not in response.text
    assert provider_key_id not in response.text

    durable = await _durable_binding(session["id"])
    assert durable == {
        "source": "platform",
        "vault_id": None,
        "model_credential_id": None,
        "organization_billing_account_id": account_id,
        "organization_provider_key_binding_id": provider_key_id,
    }
    assert await _provider_secrets(session["id"]) == {
        "OPENROUTER_API_KEY": platform_key
    }


async def test_platform_rotation_keeps_binding_id_and_updates_next_turn_key(client):
    _, provider_key_id = await _provision_platform_funding(
        policy="platform_only",
        api_key="platform-before-rotation",
    )
    agent = await _create_agent(client)
    environment = await _create_environment(client)
    response = await _create_session(
        client,
        agent_id=agent["id"],
        environment_id=environment["id"],
        funding_type="platform_credits",
    )
    assert response.status_code == 201, response.text
    session_id = response.json()["id"]
    assert await _provider_secrets(session_id) == {
        "OPENROUTER_API_KEY": "platform-before-rotation"
    }

    async with session_scope() as db:
        rotated = (
            await organization_funding_q.rotate_organization_provider_key_binding(
                db,
                provider="openrouter",
                api_key="platform-after-rotation",
            )
        )
        assert rotated.id == provider_key_id
        await db.commit()

    durable = await _durable_binding(session_id)
    assert durable["organization_provider_key_binding_id"] == provider_key_id
    assert await _provider_secrets(session_id) == {
        "OPENROUTER_API_KEY": "platform-after-rotation"
    }


@pytest.mark.parametrize("failure_mode", ("revoked", "expired", "suspended"))
async def test_bound_platform_funding_fails_closed_after_lifecycle_change(
    client,
    failure_mode,
):
    await _provision_platform_funding(
        policy="platform_only",
        api_key=f"platform-{failure_mode}",
    )
    agent = await _create_agent(client)
    environment = await _create_environment(client)
    response = await _create_session(
        client,
        agent_id=agent["id"],
        environment_id=environment["id"],
        funding_type="platform_credits",
    )
    assert response.status_code == 201, response.text
    session_id = response.json()["id"]

    async with session_scope() as db:
        if failure_mode == "revoked":
            await organization_funding_q.update_organization_provider_key_binding(
                db,
                provider="openrouter",
                status="revoked",
            )
        elif failure_mode == "expired":
            await organization_funding_q.update_organization_provider_key_binding(
                db,
                provider="openrouter",
                expires_at=datetime.now(timezone.utc) - timedelta(seconds=1),
            )
        else:
            await organization_funding_q.update_organization_billing_account(
                db,
                status="suspended",
            )
        await db.commit()

    with pytest.raises(
        ModelCredentialUnavailableError,
        match="platform credential is unavailable",
    ):
        await _provider_secrets(session_id)


async def test_platform_binding_never_falls_back_to_byok_after_session_creation(client):
    await _provision_platform_funding(
        policy="prefer_platform",
        api_key="platform-selected-once",
    )
    agent = await _create_agent(client)
    environment = await _create_environment(client)
    vault_id = await _create_vault_credential(
        client,
        api_key="byok-must-not-be-used",
    )
    response = await _create_session(
        client,
        agent_id=agent["id"],
        environment_id=environment["id"],
        vault_ids=[vault_id],
        funding_type="organization_default",
    )
    assert response.status_code == 201, response.text
    session_id = response.json()["id"]
    assert (await _durable_binding(session_id))["source"] == "platform"

    async with session_scope() as db:
        await organization_funding_q.update_organization_provider_key_binding(
            db,
            provider="openrouter",
            status="revoked",
        )
        await db.commit()

    with pytest.raises(ModelCredentialUnavailableError):
        await _provider_secrets(session_id)


async def test_platform_session_and_key_are_organization_isolated(
    client,
    database_api_key_factory,
):
    organization_id = "org_platform_isolated"
    api_key = "vma_platform_isolated_key"
    await database_api_key_factory(
        token=api_key,
        organization_id=organization_id,
    )
    headers = {**TEST_HEADERS, "x-api-key": api_key}
    _, provider_key_id = await _provision_platform_funding(
        policy="platform_only",
        api_key="isolated-platform-key",
        organization_id=organization_id,
    )
    agent = await _create_agent(client, headers=headers)
    environment = await _create_environment(client, headers=headers)
    response = await _create_session(
        client,
        agent_id=agent["id"],
        environment_id=environment["id"],
        funding_type="platform_credits",
        headers=headers,
    )
    assert response.status_code == 201, response.text
    session_id = response.json()["id"]

    cross_organization_response = await client.get(
        f"/v1/sessions/{session_id}",
        headers=TEST_HEADERS,
    )
    assert cross_organization_response.status_code == 404
    assert await _provider_secrets(
        session_id,
        organization_id=organization_id,
    ) == {"OPENROUTER_API_KEY": "isolated-platform-key"}

    async with session_scope() as db:
        with pytest.raises(
            organization_funding_q.OrganizationFundingUnavailableError
        ):
            await organization_funding_q._load_active_organization_provider_api_key(
                db,
                organization_id="org_test",
                provider="openrouter",
                provider_key_binding_id=provider_key_id,
            )
