"""The catalog and the gateway have to agree on every model."""

from __future__ import annotations

import pytest

from app.models.llm import (
    ANTHROPIC,
    DEEPSEEK,
    MODEL_CATALOG,
    MOONSHOT,
    OPENROUTER_SLUGS,
    ZAI,
)
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
    """Fourteen slugs are `f"{provider}/{id}"`; these two are not.

    Anthropic's catalog ids spell a version with hyphens and the gateway spells
    it with a dot, so deriving the slug would silently address a model that does
    not exist.
    """
    assert OPENROUTER_SLUGS["claude-sonnet-4-6"] == "anthropic/claude-sonnet-4.6"
    assert OPENROUTER_SLUGS["claude-haiku-4-5"] == "anthropic/claude-haiku-4.5"


def test_kimi_k3_uses_the_public_openrouter_id_and_reasoning_controls():
    """Kimi accepts both VMA's default effort and Maya's high one."""
    entry = next(model for model in MODEL_CATALOG if model.id == "kimi-k3")
    client = _build_chat_model(
        entry.id,
        api_key="sk-or-v1-test",
        session_id="sess_gateway_test",
    )
    high_effort = _build_chat_model(
        {"id": entry.id, "thinking": "high"},
        api_key="sk-or-v1-test",
        session_id="sess_gateway_test",
    )

    assert entry.provider == MOONSHOT
    assert entry.thinking is True
    assert OPENROUTER_SLUGS[entry.id] == "moonshotai/kimi-k3"
    assert client.model == "moonshotai/kimi-k3"
    assert client._default_params["reasoning"] == {"effort": "low"}
    assert high_effort._default_params["reasoning"] == {"effort": "high"}


def test_glm_53_flash_uses_the_public_openrouter_id():
    entry = next(model for model in MODEL_CATALOG if model.id == "glm-5.3-flash")
    client = _build_chat_model(
        entry.id,
        api_key="sk-or-v1-test",
        session_id="sess_gateway_test",
    )

    assert entry.provider == ZAI
    assert OPENROUTER_SLUGS[entry.id] == "z-ai/glm-5.3-flash"
    assert client.model == "z-ai/glm-5.3-flash"


def test_kimi_k3_is_kept_off_the_endpoint_that_degenerated():
    """Together answered two Kimi turns with thousands of `!` and a 200.

    Named here rather than left to routing because that failure is invisible
    downstream: `finish_reason` was `stop`, so no caller could reject it, and
    the endpoint's published uptime still counts those turns as served.
    """
    client = _build_chat_model(
        "kimi-k3",
        api_key="sk-or-v1-test",
        session_id="sess_gateway_test",
    )

    assert client.openrouter_provider == {
        "order": ["baseten", MOONSHOT],
        "allow_fallbacks": False,
    }


def test_an_uncatalogued_model_is_refused_before_any_client_is_built():
    with pytest.raises(UnknownModelError):
        _build_chat_model(
            "no-such-model",
            api_key="sk-or-v1-test",
            session_id="sess_gateway_test",
        )


def test_every_catalog_model_builds_one_gateway_client():
    """One client class for all of them — that is the point of the gateway.

    The credential is handed in rather than read from configuration, which is
    what lets one deployment run every Account on its own key.
    """
    built = [
        _build_chat_model(
            m.id,
            api_key="sk-or-v1-test",
            session_id="sess_gateway_test",
        )
        for m in MODEL_CATALOG
    ]

    assert {type(client).__name__ for client in built} == {"VMAChatOpenRouter"}
    assert [client.model for client in built] == [
        OPENROUTER_SLUGS[m.id] for m in MODEL_CATALOG
    ]
    assert {client.session_id for client in built} == {"sess_gateway_test"}


def test_gateway_requests_allow_ten_minutes_without_a_response():
    client = _build_chat_model(
        "deepseek-v4-pro",
        api_key="sk-or-v1-test",
        session_id="sess_gateway_test",
    )

    assert client.request_timeout == 600_000
    assert client.client.sdk_configuration.timeout_ms == 600_000


def test_only_anthropic_models_enable_automatic_prompt_caching():
    """Claude needs an explicit request-root cache control; the others do not."""
    built = {
        model.id: _build_chat_model(
            model.id,
            api_key="sk-or-v1-test",
            session_id="sess_gateway_test",
        )
        for model in MODEL_CATALOG
    }

    for model in MODEL_CATALOG:
        params = built[model.id]._default_params
        if model.provider == ANTHROPIC:
            assert params["cache_control"] == {"type": "ephemeral"}, model.id
        else:
            assert "cache_control" not in params, model.id


def test_deepseek_models_are_restricted_to_the_first_party_provider():
    """DeepSeek models must never spill to third-party inference hosts."""
    built = {
        model.id: _build_chat_model(
            model.id,
            api_key="sk-or-v1-test",
            session_id="sess_gateway_test",
        )
        for model in MODEL_CATALOG
    }

    for model in MODEL_CATALOG:
        if model.provider != DEEPSEEK:
            continue
        assert built[model.id].openrouter_provider == {
            "only": [DEEPSEEK],
            "allow_fallbacks": False,
        }, model.id


def test_models_with_nothing_said_about_routing_are_left_to_the_gateway():
    """Two providers name their endpoint. Every other model auto-routes.

    The pins are exceptions earned one at a time — a data policy for DeepSeek,
    an endpoint caught returning garbage for Kimi — so a third one appearing
    without a reason beside it should fail here.
    """
    built = {
        model.id: _build_chat_model(
            model.id,
            api_key="sk-or-v1-test",
            session_id="sess_gateway_test",
        )
        for model in MODEL_CATALOG
    }

    for model in MODEL_CATALOG:
        if model.provider in (DEEPSEEK, MOONSHOT):
            continue
        assert built[model.id].openrouter_provider is None, model.id
