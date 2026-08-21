"""The models VMA exposes through Platform and direct BYOK Accounts.

The catalog is a hard-coded product contract rather than tenant configuration.
An Account selects the inference backend and credential; the model IDs and
their public capabilities remain the same across funding modes.
"""

from typing import Literal

from app.models.common import ApiModel

ANTHROPIC = "anthropic"
DEEPSEEK = "deepseek"
GOOGLE = "google"
OPENAI = "openai"


# How hard a model is asked to think, as a Session or Agent may set it.
#
# Three, while the supported backends expose several incompatible scales.
# `minimal`, `medium` and `xhigh` are real values on some APIs and not portable
# settings here: DeepSeek and Gemini do not offer the same scale. Offering a control
# with four positions that do nothing is worse than offering one with three
# that work — a user who picks `medium` and sees no change has been told a lie
# by the interface, and there is nothing in the response to say so.
THINKING_LEVELS = ("none", "low", "high")

# What a Session gets when nobody says otherwise.
#
# Not "send nothing", which is what this used to do and is not the neutral
# option it looks like: with the field absent each upstream applies its own
# default, and DeepSeek's is thinking *on* at `high` — the slowest and priciest
# setting available, arrived at by omission. Naming `low` here is what makes the
# quiet default the cheap one.
DEFAULT_THINKING = "low"


class ModelResponse(ApiModel):
    id: str
    type: Literal["model"] = "model"
    provider: str
    display_name: str
    # Whether this model takes a thinking level at all. A `false` here does not
    # mean the model does not reason — every model below does — only that the
    # selected backend exposes no dial for it, so how deeply is the model's own
    # business.
    #
    # This is part of VMA's model contract rather than inferred from a provider
    # family: Claude Haiku 4.5 and Gemini 2.5 Pro intentionally expose no dial.
    thinking: bool = True


# Ordered strongest first within each provider, because this is the list a
# client renders in a picker. `deepseek-chat` and `deepseek-reasoner` are
# deliberately absent: DeepSeek retired both aliases on 2026-07-24 in favour of
# the two V4 ids below, and a catalog entry naming a model the provider no
# longer serves fails at the provider rather than here.
MODEL_CATALOG: tuple[ModelResponse, ...] = (
    ModelResponse(
        id="claude-opus-5",
        provider=ANTHROPIC,
        display_name="Claude Opus 5",
    ),
    ModelResponse(
        id="claude-sonnet-5",
        provider=ANTHROPIC,
        display_name="Claude Sonnet 5",
    ),
    ModelResponse(
        id="claude-sonnet-4-6",
        provider=ANTHROPIC,
        display_name="Claude Sonnet 4.6",
    ),
    ModelResponse(
        id="claude-haiku-4-5",
        provider=ANTHROPIC,
        display_name="Claude Haiku 4.5",
        thinking=False,
    ),
    ModelResponse(
        id="gemini-3.1-pro-preview",
        provider=GOOGLE,
        display_name="Gemini 3.1 Pro (preview)",
    ),
    ModelResponse(
        id="gemini-3.6-flash",
        provider=GOOGLE,
        display_name="Gemini 3.6 Flash",
    ),
    ModelResponse(
        id="gemini-3.5-flash",
        provider=GOOGLE,
        display_name="Gemini 3.5 Flash",
    ),
    ModelResponse(
        id="gemini-3.5-flash-lite",
        provider=GOOGLE,
        display_name="Gemini 3.5 Flash-Lite",
    ),
    ModelResponse(
        id="gemini-2.5-pro",
        provider=GOOGLE,
        display_name="Gemini 2.5 Pro",
        thinking=False,
    ),
    ModelResponse(
        id="gpt-5.6-sol",
        provider=OPENAI,
        display_name="GPT-5.6 Sol",
    ),
    ModelResponse(
        id="gpt-5.6-terra",
        provider=OPENAI,
        display_name="GPT-5.6 Terra",
    ),
    ModelResponse(
        id="gpt-5.6-luna",
        provider=OPENAI,
        display_name="GPT-5.6 Luna",
    ),
    # Undated on purpose. Dated snapshots were briefly listed beside these so
    # two cuts of the same model could be timed against each other; that
    # measurement is done, and a catalogue is a menu rather than a record of
    # what has been tried. Which snapshot an upstream serves for an undated id
    # is its business, and pinning one here would freeze every Session on it
    # forever.
    ModelResponse(
        id="deepseek-v4-pro",
        provider=DEEPSEEK,
        display_name="DeepSeek V4 Pro",
    ),
    ModelResponse(
        id="deepseek-v4-flash",
        provider=DEEPSEEK,
        display_name="DeepSeek V4 Flash",
    ),
)


# What OpenRouter calls each model. Written out rather than derived from
# `f"{provider}/{id}"`, which is right for twelve of these and wrong for the two
# Anthropic ids that spell their version with a dot. A wrong slug fails at the
# OpenRouter, whose error names a model nobody wrote down, so the mapping is
# stated here where a reader can check it against the catalog.
#
# Kept out of `ModelResponse` deliberately: that model is the body of
# `GET /v1/models`, and an internal OpenRouter slug is not a caller's business.
OPENROUTER_SLUGS: dict[str, str] = {
    "claude-opus-5": "anthropic/claude-opus-5",
    "claude-sonnet-5": "anthropic/claude-sonnet-5",
    "claude-sonnet-4-6": "anthropic/claude-sonnet-4.6",
    "claude-haiku-4-5": "anthropic/claude-haiku-4.5",
    "gemini-3.1-pro-preview": "google/gemini-3.1-pro-preview",
    "gemini-3.6-flash": "google/gemini-3.6-flash",
    "gemini-3.5-flash": "google/gemini-3.5-flash",
    "gemini-3.5-flash-lite": "google/gemini-3.5-flash-lite",
    "gemini-2.5-pro": "google/gemini-2.5-pro",
    "gpt-5.6-sol": "openai/gpt-5.6-sol",
    "gpt-5.6-terra": "openai/gpt-5.6-terra",
    "gpt-5.6-luna": "openai/gpt-5.6-luna",
    "deepseek-v4-pro": "deepseek/deepseek-v4-pro",
    "deepseek-v4-flash": "deepseek/deepseek-v4-flash",
}
