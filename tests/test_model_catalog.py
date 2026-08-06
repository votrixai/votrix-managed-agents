"""The catalog and the gateway have to agree on every model."""

from __future__ import annotations

import pytest

from app.models.llm import ANTHROPIC, MODEL_CATALOG, OPENROUTER_SLUGS
from app.runtime.engine import UnknownModelError, _build_chat_model


def test_every_catalog_model_has_a_gateway_slug():
    """A missing slug is only discoverable at the gateway otherwise.

    `GET /v1/models` would keep advertising the model, and the failure would
    arrive mid-turn as an error naming a model id nobody wrote down.
    """
    missing = [m.id for m in MODEL_CATALOG if m.id not in OPENROUTER_SLUGS]
    assert missing == []


def test_no_slug_survives_a_model_leaving_the_catalog():
    """The other direction: a stale slug is a model we think we still serve."""
    catalog = {m.id for m in MODEL_CATALOG}
    assert [mid for mid in OPENROUTER_SLUGS if mid not in catalog] == []


def test_slugs_are_namespaced_by_their_upstream_provider():
    """The gateway addresses a model as `vendor/model`, never bare."""
    for model_id, slug in OPENROUTER_SLUGS.items():
        assert slug.count("/") == 1, f"{model_id} -> {slug}"
        vendor, name = slug.split("/")
        assert vendor and name, f"{model_id} -> {slug}"


def test_the_two_ids_that_are_not_a_plain_join_stay_mapped():
    """Twelve slugs are `f"{provider}/{id}"`; these two are not.

    Anthropic's catalog ids spell a version with hyphens and the gateway spells
    it with a dot, so deriving the slug would silently address a model that does
    not exist.
    """
    assert OPENROUTER_SLUGS["claude-sonnet-4-6"] == "anthropic/claude-sonnet-4.6"
    assert OPENROUTER_SLUGS["claude-haiku-4-5"] == "anthropic/claude-haiku-4.5"


def test_an_uncatalogued_model_is_refused_before_any_client_is_built():
    with pytest.raises(UnknownModelError):
        _build_chat_model("no-such-model")


def test_every_catalog_model_builds_one_gateway_client(monkeypatch):
    """One client class for all of them — that is the point of the gateway."""
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-test")
    from app.config import get_settings

    get_settings.cache_clear()
    built = [_build_chat_model(m.id) for m in MODEL_CATALOG]

    assert {type(client).__name__ for client in built} == {"ChatOpenRouter"}
    assert [client.model for client in built] == [
        OPENROUTER_SLUGS[m.id] for m in MODEL_CATALOG
    ]


def test_anthropic_models_ask_for_prompt_caching(monkeypatch):
    """Anthropic bills a cached prefix only when the request asks.

    A turn resends the whole conversation, so without this the bytes the vendor
    already read are charged at full price on every tool call.
    """
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-test")
    from app.config import get_settings

    get_settings.cache_clear()

    for entry in MODEL_CATALOG:
        if entry.provider != ANTHROPIC:
            continue
        client = _build_chat_model(entry.id)
        assert client.model_kwargs == {"cache_control": {"type": "ephemeral"}}, entry.id


def test_self_caching_vendors_are_sent_no_breakpoint(monkeypatch):
    """They cache on their own, so the field would be one they have to ignore."""
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-test")
    from app.config import get_settings

    get_settings.cache_clear()

    for entry in MODEL_CATALOG:
        if entry.provider == ANTHROPIC:
            continue
        assert _build_chat_model(entry.id).model_kwargs == {}, entry.id
