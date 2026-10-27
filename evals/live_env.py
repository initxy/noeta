"""The live model settings: ``NOETA_LIVE_BASE_URL`` / ``NOETA_LIVE_API_KEY`` /
``NOETA_LIVE_MODEL`` (optional ``NOETA_LIVE_MAX_TOKENS``), from the environment
or the repo-root ``.env``; the environment wins."""

from __future__ import annotations

import os
from pathlib import Path

_ENV_PATH = Path(__file__).resolve().parent.parent / ".env"


def _load(path: Path = _ENV_PATH) -> None:
    if not path.is_file():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        if key.strip():
            os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


_load()


def live_base_url() -> str | None:
    return os.environ.get("NOETA_LIVE_BASE_URL")


def live_api_key() -> str | None:
    return os.environ.get("NOETA_LIVE_API_KEY")


def live_model() -> str | None:
    return os.environ.get("NOETA_LIVE_MODEL")


def live_max_tokens() -> int:
    raw = os.environ.get("NOETA_LIVE_MAX_TOKENS")
    return int(raw) if raw else 1024


def have_live_env() -> bool:
    return bool(live_base_url() and live_api_key() and live_model())
