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
        _build_chat_model("no-such-model", api_key="sk-or-v1-test")


def test_every_catalog_model_builds_one_gateway_client():
    """One client class for all of them — that is the point of the gateway.

    The credential is handed in rather than read from configuration, which is
    what lets one deployment run every Account on its own key.
    """
    built = [_build_chat_model(m.id, api_key="sk-or-v1-test") for m in MODEL_CATALOG]

    assert {type(client).__name__ for client in built} == {"ChatOpenRouter"}
    assert [client.model for client in built] == [
        OPENROUTER_SLUGS[m.id] for m in MODEL_CATALOG
    ]


def test_deepseek_models_are_restricted_to_the_first_party_provider():
    """DeepSeek models must never spill to third-party inference hosts."""
    built = {
        model.id: _build_chat_model(model.id, api_key="sk-or-v1-test")
        for model in MODEL_CATALOG
    }

    for model in MODEL_CATALOG:
        expected = (
            {"only": [DEEPSEEK], "allow_fallbacks": False}
            if model.provider == DEEPSEEK
            else None
        )
        assert built[model.id].openrouter_provider == expected
