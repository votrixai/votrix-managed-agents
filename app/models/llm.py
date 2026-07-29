"""The models the platform can drive.

The catalog is a hard-coded constant, not a table: the platform holds one key
per provider in config, so there is nothing per-organization to store.
"""

from typing import Literal

from app.models.common import ApiModel

ANTHROPIC = "anthropic"
DEEPSEEK = "deepseek"
GOOGLE = "google"


class ModelResponse(ApiModel):
    id: str
    type: Literal["model"] = "model"
    provider: str
    display_name: str


MODEL_CATALOG: tuple[ModelResponse, ...] = (
    ModelResponse(
        id="claude-opus-5",
        provider=ANTHROPIC,
        display_name="Claude Opus 5",
    ),
    ModelResponse(
        id="gemini-3.6-flash",
        provider=GOOGLE,
        display_name="Gemini 3.6 Flash",
    ),
    ModelResponse(
        id="deepseek-chat",
        provider=DEEPSEEK,
        display_name="DeepSeek Chat",
    ),
)
