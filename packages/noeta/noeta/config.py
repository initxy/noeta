"""Settings for the ``noeta`` command and other ready-made front ends: which
model, which provider, where tasks are stored. Read from the environment, then
``~/.noeta/config.toml``; explicit arguments win."""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from noeta.providers import provider_for
from noeta.providers.base import Provider

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

    def make_provider(self) -> Provider:
        kw: dict[str, Any] = {}
        if self.extra_headers:
            kw["extra_headers"] = dict(self.extra_headers)
        return provider_for(self.provider, base_url=self.base_url, api_key=self.api_key, **kw)

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
    }
    data.update({k: v for k, v in env.items() if v})
    data.update({k: v for k, v in overrides.items() if v is not None})
    known = {f for f in Settings.__dataclass_fields__}
    return Settings(**{k: v for k, v in data.items() if k in known})


def open_store(url: str) -> Any:
    """``sqlite:///path`` or ``postgres://…`` → a store."""
    if url.startswith("sqlite:///"):
        from noeta.store.sqlite import SqliteStore

        path = Path(url.removeprefix("sqlite:///")).expanduser()
        path.parent.mkdir(parents=True, exist_ok=True)
        return SqliteStore(str(path))
    if url.startswith(("postgres://", "postgresql://")):
        from noeta.store.postgres import PostgresStore

        return PostgresStore(url)
    raise ValueError(f"unknown store url {url!r} (sqlite:///path or postgres://...)")
