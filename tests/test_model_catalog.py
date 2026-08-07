"""The catalog and the gateway have to agree on every model."""

from __future__ import annotations

import pytest

from app.models.llm import DEEPSEEK, MODEL_CATALOG, OPENROUTER_SLUGS
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


def test_only_deepseek_models_build_while_the_gateway_is_off(monkeypatch):
    """The gateway is bypassed for now — see `_build_chat_model`.

    One client class for every model is the point of a gateway, and the slug
    table above still holds all of them for when it comes back. Until then only
    DeepSeek is reachable, and naming anything else has to fail here rather
    than at DeepSeek, which would answer about a model id nobody sent it.
    """
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test")
    from app.config import get_settings

    get_settings.cache_clear()

    deepseek = [m for m in MODEL_CATALOG if m.provider == DEEPSEEK]
    assert deepseek, "the catalog has no DeepSeek model to reach"

    built = [_build_chat_model(m.id) for m in deepseek]
    assert {type(client).__name__ for client in built} == {"ChatDeepSeek"}
    # No slug translation: the catalog id is DeepSeek's own id.
    assert [client.model_name for client in built] == [m.id for m in deepseek]

    for model in MODEL_CATALOG:
        if model.provider != DEEPSEEK:
            with pytest.raises(UnknownModelError):
                _build_chat_model(model.id)
