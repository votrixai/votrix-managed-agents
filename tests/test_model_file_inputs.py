from __future__ import annotations

import base64
import hashlib
from copy import deepcopy
from types import SimpleNamespace

import pytest
from langchain_openrouter.chat_models import _format_message_content

from app.runtime.model_inputs import adapt_user_message_content, referenced_managed_file_ids
from app.runtime.deepagents_engine import _graph_input
from app.runtime.runner import (
    _current_turn_model_file_ids,
    _runtime_context_for_session,
    _session_files_for_runtime,
    _validate_descriptor_session_files,
)
from app.runtime.sandbox_inputs import SandboxInputBundle, SandboxInputFile
from app.runtime.sandbox_lifecycle import SandboxInputMismatchError
from tests.conftest import TEST_HEADERS


PNG_BYTES = b"\x89PNG\r\n\x1a\nminimal-image"
PDF_BYTES = b"%PDF-1.7\nminimal-pdf\n%%EOF\n"
TEXT_BYTES = b"Quarterly revenue grew 12%."


def _session_file(
    *,
    source_id: str,
    scoped_id: str,
    filename: str,
    mime_type: str,
    content: bytes,
) -> dict:
    return {
        "source_file_id": source_id,
        "file_id": scoped_id,
        "filename": filename,
        "mime_type": mime_type,
        "path": f"/mnt/session/uploads/{filename}",
        "content": content,
        "read_only": True,
    }


def _file_source(file_id: str) -> dict:
    """Official Anthropic Managed Agents / current Votrix Backend shape."""
    return {"type": "file", "file_id": file_id}


def _nested_file_source(file_id: str) -> dict:
    """Defensive compatibility for clients that nest the file object."""
    return {"type": "file", "file": {"file_id": file_id}}


def _runtime_file_resource(
    *,
    source_id: str,
    scoped_id: str,
    filename: str,
    content: bytes,
) -> SimpleNamespace:
    path = f"/mnt/session/uploads/{filename}"
    return SimpleNamespace(
        id=f"resource_{scoped_id}",
        name=filename,
        data={
            "type": "file",
            "file_id": scoped_id,
            "source_file_id": source_id,
            "mount_path": path,
            "read_only": True,
            "session_file": {
                "filename": filename,
                "mime_type": "text/plain",
                "size_bytes": len(content),
                "sha256": hashlib.sha256(content).hexdigest(),
                "storage": {"key": f"objects/{filename}"},
            },
        },
    )


def test_modern_descriptor_requires_exact_session_file_metadata_before_download():
    descriptor = SandboxInputBundle(
        files=(
            SandboxInputFile(
                path="/mnt/session/uploads/report.txt",
                content=TEXT_BYTES,
                read_only=True,
                source="session_file",
            ),
        ),
        skill_sources=(),
        memory_sources=(),
        mutable_roots=(),
    ).descriptor
    matching = _runtime_file_resource(
        source_id="file_source",
        scoped_id="file_scoped",
        filename="report.txt",
        content=TEXT_BYTES,
    )
    _validate_descriptor_session_files(descriptor, [matching])

    with pytest.raises(SandboxInputMismatchError, match="file metadata"):
        _validate_descriptor_session_files(descriptor, [])
    drifted = _runtime_file_resource(
        source_id="file_source",
        scoped_id="file_scoped",
        filename="report.txt",
        content=b"drifted",
    )
    with pytest.raises(SandboxInputMismatchError, match="file metadata"):
        _validate_descriptor_session_files(descriptor, [drifted])
    extra = _runtime_file_resource(
        source_id="file_extra_source",
        scoped_id="file_extra_scoped",
        filename="extra.txt",
        content=b"extra",
    )
    with pytest.raises(SandboxInputMismatchError, match="file metadata"):
        _validate_descriptor_session_files(descriptor, [matching, extra])


def test_adapt_file_blocks_resolves_source_and_scoped_ids_without_mutating_input():
    image = _session_file(
        source_id="file_source_image",
        scoped_id="file_scoped_image",
        filename="chart.png",
        mime_type="image/png",
        content=PNG_BYTES,
    )
    pdf = _session_file(
        source_id="file_source_pdf",
        scoped_id="file_scoped_pdf",
        filename="report.pdf",
        mime_type="application/pdf",
        content=PDF_BYTES,
    )
    content = [
        {"type": "text", "text": "Compare these inputs."},
        {"type": "image", "source": _file_source("file_source_image")},
        {"type": "document", "source": _file_source("file_scoped_pdf")},
    ]
    original = deepcopy(content)

    adapted = adapt_user_message_content(
        content,
        session_files=[image, pdf],
        multimodal_input=True,
    )

    assert content == original
    assert adapted == [
        {"type": "text", "text": "Compare these inputs."},
        {
            "type": "image",
            "base64": base64.b64encode(PNG_BYTES).decode("ascii"),
            "mime_type": "image/png",
        },
        {
            "type": "file",
            "base64": base64.b64encode(PDF_BYTES).decode("ascii"),
            "mime_type": "application/pdf",
            "filename": "report.pdf",
        },
    ]


def test_adapt_accepts_defensive_nested_file_source_shape():
    image = _session_file(
        source_id="file_source_image",
        scoped_id="file_scoped_image",
        filename="chart.png",
        mime_type="image/png",
        content=PNG_BYTES,
    )

    adapted = adapt_user_message_content(
        [{"type": "image", "source": _nested_file_source("file_source_image")}],
        session_files=[image],
        multimodal_input=True,
    )

    assert adapted == [
        {
            "type": "image",
            "base64": base64.b64encode(PNG_BYTES).decode("ascii"),
            "mime_type": "image/png",
        }
    ]


def test_referenced_file_ids_extracts_only_supported_attachment_blocks():
    content = [
        {"type": "text", "text": "Compare these."},
        {"type": "image", "source": _file_source("file_image")},
        {"type": "document", "source": _nested_file_source("file_document")},
    ]

    assert referenced_managed_file_ids(content) == {
        "file_image",
        "file_document",
    }
    assert referenced_managed_file_ids("plain text") == frozenset()


def test_current_turn_file_ids_ignores_history_and_pending_action_resumes():
    history = [
        SimpleNamespace(
            seq=1,
            type="user.message",
            payload={
                "content": [{"type": "image", "source": _file_source("file_old")}]
            },
        ),
        SimpleNamespace(seq=2, type="agent.message", payload={"content": "done"}),
        SimpleNamespace(
            seq=3,
            type="user.message",
            payload={
                "content": [{"type": "document", "source": _file_source("file_current")}]
            },
        ),
    ]

    assert _current_turn_model_file_ids(history, {"last_input_event_seq": 1}) == {
        "file_current"
    }
    pending = [
        {
            "event_id": "evt_pending",
            "interrupt_id": "interrupt_1",
            "action_index": 0,
        }
    ]
    assert _current_turn_model_file_ids(
        history,
        {"last_input_event_seq": 1, "pending_actions": pending},
    ) == {"file_current"}

    resolved_history = [
        *history,
        SimpleNamespace(
            seq=4,
            type="user.tool_result",
            payload={"tool_use_id": "evt_pending", "content": "complete"},
        ),
    ]
    assert _current_turn_model_file_ids(
        resolved_history,
        {"last_input_event_seq": 1, "pending_actions": pending},
    ) == frozenset()


async def test_runtime_file_loader_downloads_only_current_turn_references(monkeypatch):
    first = _runtime_file_resource(
        source_id="file_source_first",
        scoped_id="file_scoped_first",
        filename="first.txt",
        content=b"first",
    )
    second = _runtime_file_resource(
        source_id="file_source_second",
        scoped_id="file_scoped_second",
        filename="second.txt",
        content=b"second",
    )
    objects = {
        "objects/first.txt": b"first",
        "objects/second.txt": b"second",
    }
    downloads: list[str] = []

    async def download(key: str) -> bytes:
        downloads.append(key)
        return objects[key]

    monkeypatch.setattr("app.storage.download_file", download)

    files = await _session_files_for_runtime(
        [first, second],
        content_file_ids={"file_source_second"},
        immutable_manifest={
            first.data["mount_path"]: first.data["session_file"]["sha256"],
            second.data["mount_path"]: second.data["session_file"]["sha256"],
        },
    )

    assert downloads == ["objects/second.txt"]
    assert "content" not in files[0]
    assert files[1]["content"] == b"second"
    assert files[0]["file_id"] == "file_scoped_first"


async def test_runtime_file_loader_rejects_database_metadata_outside_sealed_manifest(monkeypatch):
    mounted = _runtime_file_resource(
        source_id="file_source",
        scoped_id="file_scoped",
        filename="sealed.txt",
        content=b"sealed",
    )
    downloads: list[str] = []

    async def download(key: str) -> bytes:
        downloads.append(key)
        return b"sealed"

    monkeypatch.setattr("app.storage.download_file", download)

    with pytest.raises(RuntimeError, match="sealed manifest"):
        await _session_files_for_runtime(
            [mounted],
            content_file_ids={"file_scoped"},
            immutable_manifest={},
        )

    assert downloads == []


async def test_runtime_file_loader_plain_turn_reads_zero_objects_and_state_reads_all(monkeypatch):
    first = _runtime_file_resource(
        source_id="file_source_first",
        scoped_id="file_scoped_first",
        filename="first.txt",
        content=b"first",
    )
    second = _runtime_file_resource(
        source_id="file_source_second",
        scoped_id="file_scoped_second",
        filename="second.txt",
        content=b"second",
    )
    objects = {
        "objects/first.txt": b"first",
        "objects/second.txt": b"second",
    }
    downloads: list[str] = []

    async def download(key: str) -> bytes:
        downloads.append(key)
        return objects[key]

    monkeypatch.setattr("app.storage.download_file", download)

    metadata_only = await _session_files_for_runtime(
        [first, second],
        content_file_ids=set(),
    )
    assert downloads == []
    assert all("content" not in item for item in metadata_only)

    fully_materialized = await _session_files_for_runtime([first, second])
    assert downloads == ["objects/first.txt", "objects/second.txt"]
    assert [item["content"] for item in fully_materialized] == [b"first", b"second"]


async def test_persisted_e2b_runtime_context_does_not_load_skill_archives(monkeypatch):
    from app.runtime import runner

    session = SimpleNamespace(
        id="session_persisted",
        organization_id="org_persisted",
        runtime_thread_id="thread_persisted",
        run_state={},
    )
    persisted = {
        "provider": "e2b",
        "skill_sources": ["/skills/custom/research/v1/"],
        "memory_sources": [],
        "mutable_roots": ["/workspace", "/mnt/session/outputs"],
        "immutable_manifest": {},
    }

    async def list_resources(*_args, **_kwargs):
        return []

    async def persisted_inputs(*_args, **_kwargs):
        return persisted

    async def no_credentials(*_args, **_kwargs):
        return []

    async def no_provider_secrets(*_args, **_kwargs):
        return {}

    async def no_mcp(*_args, **_kwargs):
        return {"servers": [], "errors": []}

    async def no_subagents(*_args, **_kwargs):
        return []

    async def forbidden_skill_download(*_args, **_kwargs):
        raise AssertionError("persisted E2B turns must not hydrate Skill archives")

    monkeypatch.setattr(runner.res_q, "list_resources", list_resources)
    monkeypatch.setattr(runner, "_persisted_e2b_inputs_for_session", persisted_inputs)
    monkeypatch.setattr(runner, "_vault_credentials_for_session", no_credentials)
    monkeypatch.setattr(
        runner.session_credential_broker,
        "resolve_provider_secrets",
        no_provider_secrets,
    )
    monkeypatch.setattr(runner, "_mcp_auth_context_for_session", no_mcp)
    monkeypatch.setattr(runner, "_subagents_for_runtime", no_subagents)
    monkeypatch.setattr(runner, "_skill_archives_for_runtime", forbidden_skill_download)

    context = await _runtime_context_for_session(
        object(),
        session,
        SimpleNamespace(),
        session_file_content_ids=set(),
        prefer_persisted_sandbox_inputs=True,
    )

    assert context["skill_archives"] == []
    assert context["session_files"] == []
    assert context["persisted_sandbox_inputs"] == {
        key: value for key, value in persisted.items() if key != "immutable_manifest"
    }


def test_installed_chat_openrouter_serializes_standard_image_and_pdf_blocks():
    formatted = _format_message_content(
        [
            {
                "type": "image",
                "base64": base64.b64encode(PNG_BYTES).decode("ascii"),
                "mime_type": "image/png",
            },
            {
                "type": "file",
                "base64": base64.b64encode(PDF_BYTES).decode("ascii"),
                "mime_type": "application/pdf",
                "filename": "report.pdf",
            },
        ]
    )

    assert formatted == [
        {
            "type": "image_url",
            "image_url": {
                "url": "data:image/png;base64,"
                + base64.b64encode(PNG_BYTES).decode("ascii")
            },
        },
        {
            "type": "file",
            "file": {
                "file_data": "data:application/pdf;base64,"
                + base64.b64encode(PDF_BYTES).decode("ascii"),
                "filename": "report.pdf",
            },
        },
    ]


def test_deepagents_graph_input_uses_translated_blocks_not_public_file_ids():
    image = _session_file(
        source_id="file_source_image",
        scoped_id="file_scoped_image",
        filename="chart.png",
        mime_type="image/png",
        content=PNG_BYTES,
    )
    event = SimpleNamespace(
        seq=1,
        type="user.message",
        payload={
            "type": "user.message",
            "content": [
                {"type": "image", "source": _file_source("file_source_image")}
            ],
        },
    )

    graph_input, processed_seq = _graph_input(
        [event],
        {},
        session_files=[image],
        multimodal_input=True,
    )

    assert processed_seq == 1
    assert graph_input == {
        "messages": [
            {
                "role": "user",
                "content": [
                    {
                        "type": "image",
                        "base64": base64.b64encode(PNG_BYTES).decode("ascii"),
                        "mime_type": "image/png",
                    }
                ],
            }
        ]
    }


@pytest.mark.parametrize("multimodal_input", [True, False])
def test_adapt_plaintext_document_decodes_to_text_for_every_model(multimodal_input):
    text_file = _session_file(
        source_id="file_source_text",
        scoped_id="file_scoped_text",
        filename="notes.txt",
        mime_type="text/plain",
        content=TEXT_BYTES,
    )

    adapted = adapt_user_message_content(
        [{"type": "document", "source": _file_source("file_source_text")}],
        session_files=[text_file],
        multimodal_input=multimodal_input,
    )

    assert adapted[0]["type"] == "text"
    assert TEXT_BYTES.decode("utf-8") in adapted[0]["text"]
    assert "notes.txt" in adapted[0]["text"]


def test_adapt_non_multimodal_model_demotes_binary_files_to_sandbox_markers():
    image = _session_file(
        source_id="file_source_image",
        scoped_id="file_scoped_image",
        filename="chart.png",
        mime_type="image/png",
        content=PNG_BYTES,
    )
    pdf = _session_file(
        source_id="file_source_pdf",
        scoped_id="file_scoped_pdf",
        filename="report.pdf",
        mime_type="application/pdf",
        content=PDF_BYTES,
    )

    adapted = adapt_user_message_content(
        [
            {"type": "image", "source": _file_source("file_source_image")},
            {"type": "document", "source": _file_source("file_source_pdf")},
        ],
        session_files=[image, pdf],
        multimodal_input=False,
    )

    assert [block["type"] for block in adapted] == ["text", "text"]
    assert all("base64" not in block for block in adapted)
    assert "chart.png" in adapted[0]["text"]
    assert "image/png" in adapted[0]["text"]
    assert "/mnt/session/uploads/chart.png" in adapted[0]["text"]
    assert "report.pdf" in adapted[1]["text"]
    assert "application/pdf" in adapted[1]["text"]
    assert "/mnt/session/uploads/report.pdf" in adapted[1]["text"]


@pytest.mark.parametrize(
    ("block", "match"),
    [
        (
            {"type": "image", "source": _file_source("file_not_mounted")},
            "mounted",
        ),
        (
            {"type": "image", "source": {"type": "file", "file": {}}},
            "file_id",
        ),
        (
            {"type": "document", "source": {"type": "file"}},
            "file_id",
        ),
        (
            {
                "type": "image",
                "source": {"type": "url", "url": "https://example.com/untrusted.png"},
            },
            "file",
        ),
    ],
)
def test_adapt_rejects_unmounted_or_malformed_file_sources(block, match):
    with pytest.raises(ValueError, match=match):
        adapt_user_message_content(
            [block],
            session_files=[],
            multimodal_input=True,
        )


@pytest.mark.parametrize(
    ("block_type", "mime_type"),
    [
        ("image", "application/pdf"),
        ("document", "image/png"),
        ("document", "text/markdown"),
        ("document", "application/octet-stream"),
    ],
)
def test_adapt_rejects_block_and_mime_type_mismatches(block_type, mime_type):
    mounted = _session_file(
        source_id="file_source",
        scoped_id="file_scoped",
        filename="input.bin",
        mime_type=mime_type,
        content=b"content",
    )

    with pytest.raises(ValueError, match="MIME|mime|media"):
        adapt_user_message_content(
            [{"type": block_type, "source": _file_source("file_source")}],
            session_files=[mounted],
            multimodal_input=True,
        )


def test_adapt_demotes_non_utf8_plaintext_document_to_a_sandbox_marker():
    mounted = _session_file(
        source_id="file_source",
        scoped_id="file_scoped",
        filename="notes.txt",
        mime_type="text/plain",
        content=b"\xff\xfe",
    )

    adapted = adapt_user_message_content(
        [{"type": "document", "source": _file_source("file_source")}],
        session_files=[mounted],
        multimodal_input=False,
    )

    assert adapted[0]["type"] == "text"
    assert "notes.txt" in adapted[0]["text"]
    assert "/mnt/session/uploads/notes.txt" in adapted[0]["text"]
    assert "\ufffd" not in adapted[0]["text"]


async def _create_session(client, *, name: str):
    agent_response = await client.post(
        "/v1/agents",
        headers=TEST_HEADERS,
        json={"name": f"{name} agent", "model": {"id": "gpt-5.5"}},
    )
    assert agent_response.status_code == 201, agent_response.text
    environment_response = await client.post(
        "/v1/environments",
        headers=TEST_HEADERS,
        json={"name": f"{name} environment", "config": {"type": "cloud"}},
    )
    assert environment_response.status_code == 201, environment_response.text
    session_response = await client.post(
        "/v1/sessions",
        headers=TEST_HEADERS,
        json={
            "agent": agent_response.json()["id"],
            "environment_id": environment_response.json()["id"],
        },
    )
    assert session_response.status_code == 201, session_response.text
    return session_response.json()


async def _upload(client, *, filename: str, content: bytes, mime_type: str):
    response = await client.post(
        "/v1/files",
        headers=TEST_HEADERS,
        files={"file": (filename, content, mime_type)},
    )
    assert response.status_code == 201, response.text
    return response.json()


async def _mount(client, *, session_id: str, file_id: str, filename: str):
    response = await client.post(
        f"/v1/sessions/{session_id}/resources",
        headers=TEST_HEADERS,
        json={
            "type": "file",
            "file_id": file_id,
            "mount_path": f"/mnt/session/uploads/{filename}",
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


def _user_message(blocks: list[dict]) -> dict:
    return {"events": [{"type": "user.message", "content": blocks}]}


async def test_send_event_accepts_both_ids_for_a_file_mounted_to_the_session(client):
    session = await _create_session(client, name="mounted ids")
    uploaded = await _upload(
        client,
        filename="chart.png",
        content=PNG_BYTES,
        mime_type="image/png",
    )
    mounted = await _mount(
        client,
        session_id=session["id"],
        file_id=uploaded["id"],
        filename="chart.png",
    )
    blocks = [
        {"type": "image", "source": _file_source(uploaded["id"])},
        {"type": "image", "source": _file_source(mounted["file_id"])},
    ]

    response = await client.post(
        f"/v1/sessions/{session['id']}/events",
        headers=TEST_HEADERS,
        json=_user_message(blocks),
    )

    assert response.status_code == 200, response.text
    assert response.json()["data"][0]["content"] == blocks


async def test_send_event_rejects_an_unmounted_file_id(client):
    session = await _create_session(client, name="unmounted")
    uploaded = await _upload(
        client,
        filename="unmounted.png",
        content=PNG_BYTES,
        mime_type="image/png",
    )

    response = await client.post(
        f"/v1/sessions/{session['id']}/events",
        headers=TEST_HEADERS,
        json=_user_message(
            [{"type": "image", "source": _file_source(uploaded["id"])}]
        ),
    )

    assert response.status_code == 422, response.text
    assert "mounted" in response.json()["error"]["message"].lower()


async def test_send_event_rejects_a_file_mounted_only_to_another_session(client):
    mounted_session = await _create_session(client, name="owner")
    other_session = await _create_session(client, name="other")
    uploaded = await _upload(
        client,
        filename="private.png",
        content=PNG_BYTES,
        mime_type="image/png",
    )
    mounted = await _mount(
        client,
        session_id=mounted_session["id"],
        file_id=uploaded["id"],
        filename="private.png",
    )

    for file_id in (uploaded["id"], mounted["file_id"]):
        response = await client.post(
            f"/v1/sessions/{other_session['id']}/events",
            headers=TEST_HEADERS,
            json=_user_message(
                [{"type": "image", "source": _file_source(file_id)}]
            ),
        )

        assert response.status_code == 422, response.text
        assert "mounted" in response.json()["error"]["message"].lower()


@pytest.mark.parametrize(
    "source",
    [
        {"type": "url", "url": "https://example.com/untrusted.png"},
        {"type": "file", "file": {}},
        {"type": "file"},
        "file_source",
    ],
)
async def test_send_event_rejects_url_and_malformed_file_sources(client, source):
    session = await _create_session(client, name="malformed source")

    response = await client.post(
        f"/v1/sessions/{session['id']}/events",
        headers=TEST_HEADERS,
        json=_user_message([{"type": "image", "source": source}]),
    )

    assert response.status_code == 422, response.text


async def test_send_event_rejects_block_mime_type_mismatch(client):
    session = await _create_session(client, name="mime mismatch")
    uploaded = await _upload(
        client,
        filename="report.pdf",
        content=PDF_BYTES,
        mime_type="application/pdf",
    )
    await _mount(
        client,
        session_id=session["id"],
        file_id=uploaded["id"],
        filename="report.pdf",
    )

    response = await client.post(
        f"/v1/sessions/{session['id']}/events",
        headers=TEST_HEADERS,
        json=_user_message(
            [{"type": "image", "source": _file_source(uploaded["id"])}]
        ),
    )

    assert response.status_code == 422, response.text
    assert "mime" in response.json()["error"]["message"].lower()
