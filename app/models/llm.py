"""The models the platform can drive.

The catalog is a hard-coded constant, not a table: the platform holds one key
per provider in config, so there is nothing per-organization to store.
"""

from typing import Literal

from app.models.common import ApiModel

ANTHROPIC = "anthropic"
DEEPSEEK = "deepseek"
GOOGLE = "google"
OPENAI = "openai"


class ModelResponse(ApiModel):
    id: str
    type: Literal["model"] = "model"
    provider: str
    display_name: str


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
