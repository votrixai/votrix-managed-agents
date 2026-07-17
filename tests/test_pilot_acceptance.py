import sys
from types import SimpleNamespace

import pytest

from scripts import pilot_acceptance


class _FakeSkills:
    async def create(self, **kwargs):
        return SimpleNamespace(id="skill_smoke")

    async def delete(self, skill_id):
        return None


class _FakeAgents:
    async def create(self, **kwargs):
        return SimpleNamespace(id="agent_smoke")

    async def archive(self, agent_id):
        return None


class _FakeEnvironments:
    async def create(self, **kwargs):
        return SimpleNamespace(id="environment_smoke")

    async def delete(self, environment_id):
        return None


class _FakeFiles:
    def __init__(self):
        self.upload_count = 0

    async def upload(self, **kwargs):
        self.upload_count += 1
        return SimpleNamespace(id=f"file_{self.upload_count}")

    async def delete(self, file_id, **kwargs):
        return None


class _FakeSessionResources:
    async def add(self, session_id, **kwargs):
        return None


class _FakeSessionEvents:
    async def send(self, session_id, **kwargs):
        return SimpleNamespace(data=[SimpleNamespace(seq=1)])


class _FakeSessions:
    def __init__(self):
        self.created = []
        self.resources = _FakeSessionResources()
        self.events = _FakeSessionEvents()

    async def create(self, **kwargs):
        self.created.append(kwargs)
        return SimpleNamespace(id="session_smoke")

    async def delete(self, session_id):
        return None


class _FakeClient:
    def __init__(self):
        self.beta = SimpleNamespace(
            skills=_FakeSkills(),
            agents=_FakeAgents(),
            environments=_FakeEnvironments(),
            files=_FakeFiles(),
            sessions=_FakeSessions(),
        )

    async def close(self):
        return None


def test_resolve_vault_ids_parses_csv_and_requires_a_value():
    assert pilot_acceptance._resolve_vault_ids(
        None,
        " vault_primary, vault_fallback,,vault_primary ",
    ) == ("vault_primary", "vault_fallback")

    with pytest.raises(ValueError, match="at least one Vault ID"):
        pilot_acceptance._resolve_vault_ids(None, " , ")


def test_main_reads_smoke_vault_ids_from_environment(monkeypatch):
    received = {}

    async def fake_run(**kwargs):
        received.update(kwargs)

    monkeypatch.setattr(pilot_acceptance, "run", fake_run)
    monkeypatch.setattr(sys, "argv", ["pilot_acceptance.py"])
    monkeypatch.setenv("VMA_SMOKE_API_KEY", "smoke-secret")
    monkeypatch.setenv("VMA_SMOKE_VAULT_IDS", "vault_primary,vault_fallback")

    pilot_acceptance.main()

    assert received["vault_ids"] == ("vault_primary", "vault_fallback")


def test_main_accepts_repeated_vault_id_flags(monkeypatch):
    received = {}

    async def fake_run(**kwargs):
        received.update(kwargs)

    monkeypatch.setattr(pilot_acceptance, "run", fake_run)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "pilot_acceptance.py",
            "--vault-id",
            "vault_primary",
            "--vault-id",
            "vault_fallback",
        ],
    )
    monkeypatch.setenv("VMA_SMOKE_API_KEY", "smoke-secret")
    monkeypatch.setenv("VMA_SMOKE_VAULT_IDS", "vault_ignored")

    pilot_acceptance.main()

    assert received["vault_ids"] == ("vault_primary", "vault_fallback")


async def test_run_attaches_vault_ids_without_printing_the_api_key(monkeypatch, capsys):
    client = _FakeClient()

    async def no_wait(*args, **kwargs):
        return None

    async def output_id(*args, filename, **kwargs):
        return f"output_{filename}"

    async def no_scoped_files(*args, **kwargs):
        return []

    monkeypatch.setattr(pilot_acceptance, "AsyncAnthropic", lambda **kwargs: client)
    monkeypatch.setattr(pilot_acceptance, "_wait_for_end_turn", no_wait)
    monkeypatch.setattr(pilot_acceptance, "_assert_output", output_id)
    monkeypatch.setattr(pilot_acceptance, "_scoped_files", no_scoped_files)

    await pilot_acceptance.run(
        base_url="https://managed-agents.example.test",
        api_key="never-print-this-secret",
        model="example/model",
        timeout=1,
        vault_ids=("vault_primary", "vault_primary", " vault_fallback "),
    )

    assert client.beta.sessions.created[0]["vault_ids"] == [
        "vault_primary",
        "vault_fallback",
    ]
    captured = capsys.readouterr()
    assert "never-print-this-secret" not in captured.out
    assert "never-print-this-secret" not in captured.err
