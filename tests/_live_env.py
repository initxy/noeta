"""Shared live-LLM test configuration and provider factories.

Live tests (``@pytest.mark.live``) hit a real gateway and are skipped in CI.
Rather than each file inventing its own env contract, this module is the single
source of truth: it loads a repo-root ``.env`` (git-ignored, holds the rotated
key), exposes the unified ``NOETA_LIVE_*`` variables, and builds each shipping
provider adapter against them.

One gateway, one key, one base — three endpoints::

    NOETA_LIVE_BASE_URL   e.g. https://gateway.example.com
    NOETA_LIVE_API_KEY    the rotated, human-held key
    NOETA_LIVE_MODEL      the model id to drive
    NOETA_LIVE_MAX_TOKENS   optional, default 1024
    NOETA_LIVE_VISION_MODEL optional; a vision-capable model for the image chain

Copy ``.env.example`` to ``.env`` and fill it in, then::

    uv run pytest -m live

Missing any of base/key/model auto-skips every live test. The loader is
stdlib-only on purpose — this is a pure-library repo and ``python-dotenv`` is
not a dependency.

The three factories reuse the shipping adapters unchanged. The one wrinkle is
the Responses endpoint: the adapter sends an Azure-style ``api-key`` header, but
a gateway fronting the OpenAI ``/v1/responses`` shape may want
``Authorization: Bearer``. The factory injects that through the adapter's
``extra_headers`` escape hatch — no adapter code changes, and the surplus
``api-key`` header is simply ignored by such a gateway.
"""

from __future__ import annotations

import os
import struct
import zlib
from pathlib import Path
from typing import Any, Optional

import pytest


# --------------------------------------------------------------------------- #
# .env loading (stdlib-only)
# --------------------------------------------------------------------------- #

#: Repo root holds ``.env`` — this file is ``<root>/tests/_live_env.py``.
_ENV_PATH = Path(__file__).resolve().parent.parent / ".env"


def load_dotenv(path: Path = _ENV_PATH) -> None:
    """Load ``KEY=VALUE`` lines from ``path`` into ``os.environ``.

    Idempotent and non-clobbering: an already-set environment variable wins
    over the file (``setdefault``), so an explicit ``NOETA_LIVE_MODEL=... uv run
    pytest`` still overrides the ``.env``. Blank lines, ``#`` comments, and
    surrounding quotes are handled; malformed lines are skipped silently rather
    than failing collection. Missing file is a no-op.
    """
    if not path.is_file():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key:
            os.environ.setdefault(key, value)


# Load once at import so every live module sees the same environment.
load_dotenv()


# --------------------------------------------------------------------------- #
# Unified variable getters
# --------------------------------------------------------------------------- #


def live_base_url() -> Optional[str]:
    return os.environ.get("NOETA_LIVE_BASE_URL")


def live_api_key() -> Optional[str]:
    return os.environ.get("NOETA_LIVE_API_KEY")


def live_model() -> Optional[str]:
    return os.environ.get("NOETA_LIVE_MODEL")


def live_max_tokens() -> int:
    raw = os.environ.get("NOETA_LIVE_MAX_TOKENS")
    return int(raw) if raw else 1024


def live_vision_model() -> Optional[str]:
    """A vision-capable model id, if the gateway exposes one distinctly.

    Falls back to ``NOETA_LIVE_MODEL`` so a single multimodal model needs only
    the one variable; the image chain skips itself when neither is set.
    """
    return os.environ.get("NOETA_LIVE_VISION_MODEL") or live_model()


def have_live_env() -> bool:
    """True iff base + key + model are all present."""
    return bool(live_base_url() and live_api_key() and live_model())


#: Shared skip guard — import and apply to every live test module/function.
requires_live = pytest.mark.skipif(
    not have_live_env(),
    reason=(
        "live LLM tests need NOETA_LIVE_BASE_URL / NOETA_LIVE_API_KEY / "
        "NOETA_LIVE_MODEL (copy .env.example to .env). Skipped in CI."
    ),
)


# --------------------------------------------------------------------------- #
# Network opt-in — the web tools hit the real internet
# --------------------------------------------------------------------------- #


def have_live_web() -> bool:
    """True iff the caller opted into real-network web tests (``NOETA_LIVE_WEB``).

    Kept separate from :func:`have_live_env`: the rest of the live suite talks
    only to the configured gateway, but the web tools reach arbitrary public
    hosts, so hitting the network is a second, explicit opt-in — a developer
    with a ``.env`` still does not fetch example.com unless they ask for it.
    """
    return os.environ.get("NOETA_LIVE_WEB", "").strip() not in ("", "0", "false")


def have_web_search_key() -> bool:
    """True iff ``NOETA_WEB_SEARCH_API_KEY`` is set — the WebSearch on/off switch.

    Without it the ``WebSearch`` tool is not even built into the tool set
    (``noeta.builtins.web.impl.search`` gates on it), so a WebSearch live test
    has nothing to drive and must skip.
    """
    return bool(os.environ.get("NOETA_WEB_SEARCH_API_KEY", "").strip())


#: Guard for the WebFetch chain — needs the gateway env AND the network opt-in.
requires_live_web = pytest.mark.skipif(
    not (have_live_env() and have_live_web()),
    reason=(
        "web live tests hit the real network — set NOETA_LIVE_WEB=1 (plus the "
        "usual NOETA_LIVE_* gateway env) to opt in. Skipped in CI and by default."
    ),
)

#: Guard for the WebSearch chain — additionally needs the search API key.
requires_live_web_search = pytest.mark.skipif(
    not (have_live_env() and have_live_web() and have_web_search_key()),
    reason=(
        "WebSearch live test needs NOETA_LIVE_WEB=1 AND NOETA_WEB_SEARCH_API_KEY "
        "(the tool is omitted from the set without the key). Skipped otherwise."
    ),
)


# --------------------------------------------------------------------------- #
# Provider factories — reuse the shipping adapters unchanged
# --------------------------------------------------------------------------- #


def build_anthropic_provider() -> Any:
    """Anthropic ``/v1/messages`` adapter against the gateway base."""
    from noeta.builtins.providers.impl.anthropic import AnthropicProvider

    return AnthropicProvider(
        api_key=live_api_key(),
        base_url=live_base_url() or "",
        default_max_tokens=live_max_tokens(),
    )


def build_responses_provider(content_store: Optional[Any] = None) -> Any:
    """OpenAI ``/v1/responses`` adapter against ``<base>/v1/responses``.

    ``base_url`` for this adapter is the **complete** endpoint (POSTed verbatim).
    A ``Bearer`` header is injected via ``extra_headers`` because a gateway
    fronting this shape may reject the adapter's default ``api-key`` header.
    """
    from noeta.builtins.providers.impl.openai_responses import OpenAIResponsesProvider

    key = live_api_key() or ""
    base = (live_base_url() or "").rstrip("/")
    return OpenAIResponsesProvider(
        base_url=f"{base}/v1/responses",
        api_key=key,
        extra_headers={"Authorization": f"Bearer {key}"},
        image_resolver=content_store.get if content_store is not None else None,
    )


def build_openai_compat_provider() -> Any:
    """OpenAI ``/v1/chat/completions`` adapter against ``<base>/v1``.

    The adapter appends ``/chat/completions`` and already sends
    ``Authorization: Bearer``, so no header injection is needed.
    """
    from noeta.builtins.providers.impl.openai_compat import OpenAICompatProvider

    base = (live_base_url() or "").rstrip("/")
    return OpenAICompatProvider(base_url=f"{base}/v1", api_key=live_api_key())


# --------------------------------------------------------------------------- #
# Shared image fixture
# --------------------------------------------------------------------------- #


def solid_png(width: int, height: int, rgba: tuple[int, int, int, int]) -> bytes:
    """Generate a solid-color RGBA PNG at a fixed zlib level, so the bytes are
    reproducible across runs and machines.

    A **1x1 degenerate image will not work**: gateways reject it with HTTP 400
    ("The image data you provided does not represent a valid image."), so the
    fixture needs real dimensions.
    """
    raw = b"".join(b"\x00" + bytes(rgba) * width for _ in range(height))

    def _chunk(tag: bytes, data: bytes) -> bytes:
        body = tag + data
        return (
            struct.pack(">I", len(data))
            + body
            + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF)
        )

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + _chunk(b"IHDR", ihdr)
        + _chunk(b"IDAT", zlib.compress(raw, 9))
        + _chunk(b"IEND", b"")
    )


#: A 32x32 solid-red PNG for the image chains.
SAMPLE_PNG = solid_png(32, 32, (220, 40, 40, 255))
