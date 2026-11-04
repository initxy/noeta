"""A provider's own model listing (``GET …/models``): which models it serves
and, where it publishes them, their context windows and output caps.

Read only from the endpoint a provider is configured with, with its own key.
Every key a vendor or gateway is known to publish a limit under is read
(OpenAI-compatible gateways, OpenRouter, vLLM, Anthropic's ``max_input_tokens``);
a field that is absent stays ``None``.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

import httpx

__all__ = ["ListedModel", "fetch_listing", "listing_url", "parse_listing"]

#: How long one listing request may take.
LISTING_TIMEOUT_SECONDS = 5.0
_ANTHROPIC_VERSION = "2023-06-01"
_WINDOW_KEYS = ("context_length", "context_window", "max_input_tokens", "max_model_len",
                "max_context_length", "input_token_limit")
_OUTPUT_KEYS = ("max_output_tokens", "max_completion_tokens", "output_token_limit")


@dataclass(frozen=True)
class ListedModel:
    id: str
    context_window: int | None = None
    max_output_tokens: int | None = None
    #: ``True`` / ``False`` when the listing names the input modalities.
    supports_vision: bool | None = None


def _int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)) and value > 0:
        return int(value)
    if isinstance(value, str) and value.isdigit() and int(value) > 0:
        return int(value)
    return None


def _first(row: Mapping[str, Any], keys: tuple[str, ...]) -> int | None:
    for key in keys:
        found = _int(row.get(key))
        if found is not None:
            return found
    return None


def _table(row: Mapping[str, Any], key: str) -> Mapping[str, Any]:
    found = row.get(key)
    return found if isinstance(found, Mapping) else {}


def _row(row: Mapping[str, Any], family: str) -> ListedModel | None:
    model = row.get("id")
    if not isinstance(model, str) or not model:
        return None
    top = _table(row, "top_provider")
    window = _first(row, _WINDOW_KEYS) or _first(top, ("context_length",))
    output = _first(row, _OUTPUT_KEYS) or _first(top, ("max_completion_tokens",))
    if output is None and family == "anthropic":
        # Anthropic's listing names the output cap ``max_tokens``.
        output = _int(row.get("max_tokens"))
    arch = _table(row, "architecture")
    modalities = row.get("input_modalities") or arch.get("input_modalities") \
        or row.get("modalities")
    vision = ("image" in modalities) if isinstance(modalities, list) else None
    return ListedModel(model, window, output, vision)


def parse_listing(payload: Any, family: str = "") -> list[ListedModel]:
    """The models of one listing page (``{"data": [...]}``, ``{"models": [...]}``
    or a bare list); rows without an id are skipped."""
    rows = payload
    if isinstance(payload, Mapping):
        rows = payload.get("data", payload.get("models", []))
    if not isinstance(rows, list):
        return []
    out = [_row(r, family) for r in rows if isinstance(r, Mapping)]
    return [m for m in out if m is not None]


def listing_url(family: str, base_url: str | None) -> str | None:
    """Where ``family``'s listing is, given the provider's ``base_url``;
    ``None`` for a family noeta knows no listing of."""
    if family == "anthropic":
        return (base_url or "https://api.anthropic.com").rstrip("/") + "/v1/models"
    if family == "openai_compat" and base_url:
        return base_url.rstrip("/") + "/models"
    if family == "openai_responses" and base_url:
        root = base_url.rstrip("/")
        if root.endswith("/responses"):
            return root.removesuffix("/responses") + "/models"
    return None


def _headers(family: str, api_key: str | None, extra: Mapping[str, str]) -> dict[str, str]:
    if family == "anthropic":
        key = api_key or os.environ.get("ANTHROPIC_API_KEY") or ""
        headers = {"x-api-key": key, "anthropic-version": _ANTHROPIC_VERSION}
    elif family == "openai_responses":
        headers = {"api-key": api_key or ""}
    else:
        key = api_key or os.environ.get("OPENAI_API_KEY") or ""
        headers = {"Authorization": f"Bearer {key}"} if key else {}
    headers.update(extra)
    return headers


def fetch_listing(family: str, base_url: str | None, api_key: str | None,
                  headers: Mapping[str, str] | None = None, *,
                  timeout: float = LISTING_TIMEOUT_SECONDS,
                  transport: httpx.BaseTransport | None = None) -> list[ListedModel]:
    """The provider's listing, every page of it. Raises ``httpx.HTTPError``
    or ``ValueError`` (no listing for this family, a body that is not JSON)."""
    url = listing_url(family, base_url)
    if url is None:
        raise ValueError(f"no model listing is known for the {family} family")
    out: list[ListedModel] = []
    params: dict[str, Any] = {"limit": 1000} if family == "anthropic" else {}
    with httpx.Client(timeout=timeout, transport=transport,
                      headers=_headers(family, api_key, headers or {})) as client:
        for _page in range(50):
            response = client.get(url, params=params)
            response.raise_for_status()
            payload = response.json()
            out += parse_listing(payload, family)
            last = payload.get("last_id") if isinstance(payload, Mapping) else None
            if not (isinstance(payload, Mapping) and payload.get("has_more") and last):
                break
            params = {**params, "after_id": last}
    return out
