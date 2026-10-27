"""Settings for the ``noeta`` command and other ready-made front ends: which
model, which provider, where tasks are stored. Read from the environment, then
``~/.noeta/config.toml``; explicit arguments win."""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from noeta.core import registry
from noeta.core.provider import Provider

HOME = Path(os.environ.get("NOETA_HOME") or Path.home() / ".noeta")


@dataclass
class Settings:
    model: str = ""
    #: ``anthropic`` | ``openai_compat`` | ``openai_responses``.
    provider: str = "anthropic"
    base_url: str | None = None
    api_key: str | None = None
    #: ``sqlite:///path`` or ``postgres://...``.
    store: str = ""
    extra_headers: dict[str, str] = field(default_factory=dict)
    effort: str | None = None
    #: A JSON file of extra models: ``{"models": [{"id", "context_window",
    #: "max_output_tokens", "supports_vision"?, "family"?}]}``.
    models_file: str | None = None

    def make_provider(self) -> Provider:
        kw: dict[str, Any] = {}
        if self.extra_headers:
            kw["extra_headers"] = dict(self.extra_headers)
        return registry.provider(self.provider, base_url=self.base_url, api_key=self.api_key,
                                 **kw)

    @property
    def store_url(self) -> str:
        return self.store or f"sqlite:///{HOME / 'tasks.db'}"


def load_settings(**overrides: Any) -> Settings:
    data: dict[str, Any] = {}
    path = HOME / "config.toml"
    if path.is_file():
        data.update(tomllib.loads(path.read_text("utf-8")))
    env = {
        "model": os.environ.get("NOETA_MODEL"),
        "provider": os.environ.get("NOETA_PROVIDER"),
        "base_url": os.environ.get("NOETA_BASE_URL"),
        "api_key": os.environ.get("NOETA_API_KEY"),
        "store": os.environ.get("NOETA_STORE"),
        "effort": os.environ.get("NOETA_EFFORT"),
        "models_file": os.environ.get("NOETA_MODELS_FILE"),
    }
    data.update({k: v for k, v in env.items() if v})
    data.update({k: v for k, v in overrides.items() if v is not None})
    known = {f for f in Settings.__dataclass_fields__}
    settings = Settings(**{k: v for k, v in data.items() if k in known})
    if settings.models_file:
        register_models_file(settings.models_file)
    return settings


def register_models_file(path: str) -> None:
    """Register the models a JSON file describes (gateway models the shipped
    catalog does not know), so they run at their real limits."""
    import json

    from noeta.core.models import ModelSpec, find_spec, register_models

    rows = json.loads(Path(path).expanduser().read_text("utf-8"))
    rows = rows.get("models", rows) if isinstance(rows, dict) else rows
    specs = {}
    for row in rows:
        if not row.get("context_window") or not row.get("max_output_tokens"):
            continue
        if find_spec(row["id"]) is not None:
            continue
        specs[row["id"]] = ModelSpec(
            real_model_id=row["id"], context_window=int(row["context_window"]),
            max_output_tokens=int(row["max_output_tokens"]),
            supports_vision=bool(row.get("supports_vision", False)),
            is_reasoning=bool(row.get("efforts")), family=row.get("family"))
    if specs:
        register_models(specs)


def open_store(url: str) -> Any:
    """``sqlite:///path`` or ``postgres://…`` → a store."""
    return registry.store(url)
