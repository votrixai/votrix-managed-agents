import pytest

from app.db.engine import session_scope
from app.db.queries import agents as agents_q
from app.db.queries import session_funding_bindings as funding_q
from app.db.queries import sessions as sessions_q
from app.runtime.agent_resolution import effective_agent_version
from app.runtime.credential_broker import session_credential_broker
from app.runtime.model_credentials import ModelCredentialUnavailableError
from app.runtime.runner import _runtime_context_for_session
from app.runtime.sandbox_lifecycle import build_session_input_bundle
from tests.conftest import TEST_HEADERS


async def _agent(client):
    response = await client.post(
        "/v1/agents",
        headers=TEST_HEADERS,
        json={
            "name": "Frozen model credential",
            "model": {"id": "deepseek/deepseek-v4-pro", "provider": "openrouter"},
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


async def _environment(client):
    response = await client.post(
        "/v1/environments",
        headers=TEST_HEADERS,
        json={"name": "frozen-model-credential", "config": {"type": "cloud"}},
    )
    assert response.status_code == 201, response.text
    return response.json()


async def _vault(client, name):
    response = await client.post("/v1/vaults", headers=TEST_HEADERS, json={"display_name": name})
    assert response.status_code == 201, response.text
    return response.json()


async def _credential(client, vault_id, value):
    response = await client.post(
        f"/v1/vaults/{vault_id}/credentials",
        headers=TEST_HEADERS,
        json={
            "display_name": "OpenRouter",
            "auth": {
                "type": "environment_variable",
                "secret_name": "OPENROUTER_API_KEY",
                "secret_value": value,
                "networking": {"type": "unrestricted"},
            },
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


async def _session(client, agent, environment, vault_ids):
    response = await client.post(
        "/v1/sessions",
        headers=TEST_HEADERS,
        json={
            "agent": agent["id"],
            "environment_id": environment["id"],
            "vault_ids": vault_ids,
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


async def _runtime_context(session_id, *, commit=False):
    async with session_scope() as db:
        session = await sessions_q.get_session(db, session_id)
        version = await agents_q.get_agent_version(
            db,
            agent_id=session.agent_id,
            version=session.agent_version,
            organization_id=session.organization_id,
        )
        context = await _runtime_context_for_session(
            db,
            session,
            effective_agent_version(version, session.status_details),
        )
        if commit:
            await db.commit()
        return context


async def test_session_requires_matching_vault_model_credential(client, monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "process-environment-must-not-be-used")
    agent = await _agent(client)
    environment = await _environment(client)
    vault = await _vault(client, "Initially empty")

    missing = await client.post(
        "/v1/sessions",
        headers=TEST_HEADERS,
        json={
            "agent": agent["id"],
            "environment_id": environment["id"],
            "vault_ids": [vault["id"]],
        },
    )
    assert missing.status_code == 422, missing.text
    assert missing.json()["error"]["code"] == "model_credential_required"
    assert "openrouter" in missing.json()["error"]["message"]

    credential = await _credential(client, vault["id"], "added-later")
    session = await _session(client, agent, environment, [vault["id"]])
    assert session["status_details"]["model_credential_binding"]["source"] == "vault"
    assert session["status_details"]["model_credential_binding"]["model_provider"] == "openrouter"
    assert "secret_name" not in session["status_details"]["model_credential_binding"]
    assert (await _runtime_context(session["id"]))["provider_secrets"] == {
        "OPENROUTER_API_KEY": "added-later"
    }

    response = await client.post(
        f"/v1/vaults/{vault['id']}/credentials/{credential['id']}",
        headers=TEST_HEADERS,
        json={
            "auth": {
                "type": "environment_variable",
                "secret_value": "rotated-in-place",
            }
        },
    )
    assert response.status_code == 200, response.text
    assert (await _runtime_context(session["id"]))["provider_secrets"] == {
        "OPENROUTER_API_KEY": "rotated-in-place"
    }


async def test_session_persists_one_immutable_funding_binding(client):
    agent = await _agent(client)
    environment = await _environment(client)
    vault = await _vault(client, "Organization funding")
    credential = await _credential(client, vault["id"], "organization-key")
    session = await _session(client, agent, environment, [vault["id"]])

    async with session_scope() as db:
        binding = await funding_q.get_session_funding_binding(
            db,
            session["id"],
            organization_id="org_test",
        )
        assert binding is not None
        assert binding.source == "vault"
        assert binding.provider == "openrouter"
        assert binding.model_id == "deepseek/deepseek-v4-pro"
        assert binding.vault_id == vault["id"]
        assert binding.model_credential_id == credential["id"]

        replay = await funding_q.create_session_funding_binding(
            db,
            session_id=session["id"],
            source="vault",
            provider="openrouter",
            model_id="deepseek/deepseek-v4-pro",
            vault_id=vault["id"],
            model_credential_id=credential["id"],
            organization_id="org_test",
        )
        assert replay.id == binding.id

        with pytest.raises(funding_q.SessionFundingBindingConflictError):
            await funding_q.create_session_funding_binding(
                db,
                session_id=session["id"],
                source="vault",
                provider="openrouter",
                model_id="another-model",
                vault_id=vault["id"],
                model_credential_id=credential["id"],
                organization_id="org_test",
            )


async def test_legacy_server_binding_is_rejected(client):
    agent = await _agent(client)
    environment = await _environment(client)
    vault = await _vault(client, "Legacy server binding")
    await _credential(client, vault["id"], "organization-owned")
    session = await _session(client, agent, environment, [vault["id"]])

    async with session_scope() as db:
        record = await sessions_q.get_session(db, session["id"])
        details = dict(record.status_details)
        details["model_credential_binding"] = {
            "version": 1,
            "source": "server",
            "credential_id": None,
            "vault_id": None,
            "model_provider": "openrouter",
            "secret_name": "OPENROUTER_API_KEY",
        }
        await sessions_q.update_session(db, record, status_details=details)
        await db.commit()

    with pytest.raises(ModelCredentialUnavailableError, match="Server-provided"):
        await _runtime_context(session["id"])


async def test_legacy_session_binds_once_then_revocation_fails_closed(client):
    agent = await _agent(client)
    environment = await _environment(client)
    personal = await _vault(client, "Personal")
    shared = await _vault(client, "Shared")
    credential = await _credential(client, personal["id"], "personal")
    await _credential(client, shared["id"], "shared")
    session = await _session(client, agent, environment, [personal["id"], shared["id"]])

    # Simulate a row created before either durable binding representation existed.
    async with session_scope() as db:
        record = await sessions_q.get_session(db, session["id"])
        details = dict(record.status_details)
        details.pop("model_credential_binding")
        await sessions_q.update_session(db, record, status_details=details)
        durable = await funding_q.get_session_funding_binding(
            db,
            session["id"],
            organization_id=record.organization_id,
        )
        assert durable is not None
        await db.delete(durable)
        await db.commit()

    assert (await _runtime_context(session["id"], commit=True))["provider_secrets"] == {
        "OPENROUTER_API_KEY": "personal"
    }
    async with session_scope() as db:
        record = await sessions_q.get_session(db, session["id"])
        assert record.status_details["model_credential_binding"]["credential_id"] == credential["id"]
        durable = await funding_q.get_session_funding_binding(
            db,
            session["id"],
            organization_id=record.organization_id,
        )
        assert durable is not None
        assert durable.model_credential_id == credential["id"]

    response = await client.post(f"/v1/vaults/{personal['id']}/archive", headers=TEST_HEADERS)
    assert response.status_code == 200, response.text
    with pytest.raises(ModelCredentialUnavailableError, match="create a new Session"):
        await _runtime_context(session["id"])


async def test_sandbox_input_materialization_does_not_resolve_model_secrets(
    client,
    monkeypatch,
):
    agent = await _agent(client)
    environment = await _environment(client)
    vault = await _vault(client, "Organization model credentials")
    secret = "must-remain-in-the-control-plane"
    await _credential(client, vault["id"], secret)
    session = await _session(client, agent, environment, [vault["id"]])

    async def unexpected_model_secret_resolution(*_args, **_kwargs):
        raise AssertionError("Sandbox materialization must not resolve model secrets")

    monkeypatch.setattr(
        session_credential_broker,
        "resolve_provider_secrets",
        unexpected_model_secret_resolution,
    )
    async with session_scope() as db:
        record = await sessions_q.get_session(db, session["id"])
        version = await agents_q.get_agent_version(
            db,
            agent_id=record.agent_id,
            version=record.agent_version,
            organization_id=record.organization_id,
        )
        bundle = await build_session_input_bundle(db, record, version)

    assert secret not in repr(bundle)
