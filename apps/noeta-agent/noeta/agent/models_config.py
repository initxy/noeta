"""Model definitions: reads the available models and effort options from the
project-root ``models.json``.

Deep module: callers touch only ``get_models`` / ``get_default_model`` /
``validate_model``; file parsing, fallback, and path resolution all hide
behind this layer.

Fallback policy: when the config file is missing or unparseable, degrade to a
single model ``gpt-5.5-2026-04-24`` (label "GPT 5.5") and log a warning — the
backend never crashes over model config; plain conversation keeps working.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from noeta.agent.config import Settings

logger = logging.getLogger(__name__)

# Single-model fallback (missing / unparseable config file).
_FALLBACK_ID = "gpt-5.5-2026-04-24"
_FALLBACK_LABEL = "GPT 5.5"


class ModelDef(BaseModel):
    id: str
    label: str
    default: bool = False
    efforts: list[str] = Field(default_factory=list)
    default_effort: str | None = None
    # Which LLM gateway serves this model (backend routing only; not part of
    # the /models contract). Default "openai" (the primary gateway); models
    # served by the secondary gateway are tagged "secondary", and
    # build_provider dispatches their requests there (different base_url +
    # Bearer auth). See providers.py / routing_provider.py.
    gateway: str = "openai"
    # Context window / output reservation (tokens). noeta's
    # derive_compaction_config only enables context compaction for models in
    # the SDK catalog; custom-gateway models the SDK does not know need these
    # two values so build_provider can register a ModelSpec — otherwise
    # compaction stays off and the context grows without bound. None = do not
    # register a spec (SDK-known models ship authoritative rows already).
    context_window: int | None = None
    max_output_tokens: int | None = None

    def to_api(self) -> dict[str, Any]:
        """The /models endpoint serialization (stable field order for
        contract tests).

        Excludes gateway / context_window / max_output_tokens: those are
        backend routing/compaction internals the frontend selector neither
        needs nor should see.
        """
        return {
            "id": self.id,
            "label": self.label,
            "default": self.default,
            "efforts": list(self.efforts),
            "default_effort": self.default_effort,
        }


def _fallback() -> list[ModelDef]:
    return [ModelDef(id=_FALLBACK_ID, label=_FALLBACK_LABEL, default=True)]


def _load(path: Path) -> list[ModelDef]:
    if not path.is_file():
        logger.warning("models config missing, degrading to a single model: %s", path)
        return _fallback()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        defs = [ModelDef(**m) for m in raw.get("models", [])]
    except Exception:  # noqa: BLE001 - any parse error degrades, never crashes
        logger.warning(
            "models config unparseable, degrading to a single model: %s",
            path,
            exc_info=True,
        )
        return _fallback()
    if not defs:
        logger.warning("models config empty, degrading to a single model: %s", path)
        return _fallback()
    # With multiple default=True entries only the first counts; the rest are
    # demoted so /models never advertises two defaults.
    seen_default = False
    for d in defs:
        if d.default:
            if seen_default:
                d.default = False
            else:
                seen_default = True
    return defs


def get_models(settings: Settings) -> list[ModelDef]:
    return _load(settings.models_config_path)


def get_default_model(settings: Settings) -> ModelDef:
    models = get_models(settings)
    for m in models:
        if m.default:
            return m
    return models[0]


def validate_model(settings: Settings, model_id: str) -> str:
    """Validate that model_id is in the list and return it; raise ValueError
    otherwise."""
    for m in get_models(settings):
        if m.id == model_id:
            return model_id
    raise ValueError(f"unknown model: {model_id}")
