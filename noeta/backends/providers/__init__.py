"""LLM provider adapters: ``anthropic``, ``openai_compat``, ``openai_responses``.

Adapters are imported lazily by :func:`provider_for`, so importing this
package does not import ``httpx`` for a caller that only wants the interface.
"""

from __future__ import annotations

from typing import Any

from noeta.core.provider import OnDelta, Provider

__all__ = ["FAMILIES", "OnDelta", "Provider", "anthropic", "openai_compat", "openai_responses",
           "provider_for"]

#: Wire families :func:`provider_for` knows.
FAMILIES = ("anthropic", "openai_compat", "openai_responses")


def provider_for(
    family: str,
    *,
    base_url: str | None = None,
    api_key: str | None = None,
    **kw: Any,
) -> Provider:
    """Build the adapter for ``family``. Extra keywords go to its constructor.

    * ``anthropic`` — ``base_url`` defaults to ``https://api.anthropic.com``;
      ``api_key`` to ``$ANTHROPIC_API_KEY``.
    * ``openai_compat`` — ``base_url`` is the API root that
      ``/chat/completions`` is appended to (required); ``api_key`` defaults to
      ``$OPENAI_API_KEY``.
    * ``openai_responses`` — ``base_url`` is the complete responses endpoint,
      POSTed verbatim (required); ``api_key`` is required.
    """
    if family == "anthropic":
        from noeta.backends.providers.anthropic import AnthropicProvider

        return AnthropicProvider(api_key, base_url=base_url, **kw)
    if family == "openai_compat":
        from noeta.backends.providers.openai_compat import OpenAICompatProvider

        if not base_url:
            raise ValueError("openai_compat needs base_url (the API root, e.g. .../v1)")
        return OpenAICompatProvider(base_url, api_key, **kw)
    if family == "openai_responses":
        from noeta.backends.providers.openai_responses import OpenAIResponsesProvider

        if not base_url or not api_key:
            raise ValueError(
                "openai_responses needs base_url (the complete responses endpoint) "
                "and api_key"
            )
        return OpenAIResponsesProvider(base_url, api_key, **kw)
    raise ValueError(f"unknown provider family {family!r}; expected one of {FAMILIES}")


# The ``noeta.providers`` entry points, one per family.


def anthropic(**kw: Any) -> Provider:
    return provider_for("anthropic", **kw)


def openai_compat(**kw: Any) -> Provider:
    return provider_for("openai_compat", **kw)


def openai_responses(**kw: Any) -> Provider:
    return provider_for("openai_responses", **kw)
