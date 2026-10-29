"""LLM provider adapters: ``anthropic``, ``openai_compat``, ``openai_responses``.

Adapters are imported lazily by :func:`provider_for`, so importing this
package does not import ``httpx`` for a caller that only wants the interface.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from noeta.core.provider import OnDelta, Provider

__all__ = ["FAMILIES", "OnDelta", "Provider", "anthropic_provider", "openai_compat_provider",
           "openai_responses_provider", "provider_for"]


def anthropic_provider(*, base_url: str | None = None, api_key: str | None = None,
                       **kw: Any) -> Provider:
    from noeta.backends.providers.anthropic import AnthropicProvider

    return AnthropicProvider(api_key, base_url=base_url, **kw)


def openai_compat_provider(*, base_url: str | None = None, api_key: str | None = None,
                           **kw: Any) -> Provider:
    from noeta.backends.providers.openai_compat import OpenAICompatProvider

    if not base_url:
        raise ValueError("openai_compat needs base_url (the API root, e.g. .../v1)")
    return OpenAICompatProvider(base_url, api_key, **kw)


def openai_responses_provider(*, base_url: str | None = None, api_key: str | None = None,
                              **kw: Any) -> Provider:
    from noeta.backends.providers.openai_responses import OpenAIResponsesProvider

    if not base_url or not api_key:
        raise ValueError(
            "openai_responses needs base_url (the complete responses endpoint) "
            "and api_key"
        )
    return OpenAIResponsesProvider(base_url, api_key, **kw)


# The ``noeta.providers`` entry points are the constructors above, one per
# family. Not named like the family: importing the submodule ``anthropic``
# rebinds the package's ``anthropic`` attribute to that module.
_FACTORIES: dict[str, Callable[..., Provider]] = {
    "anthropic": anthropic_provider,
    "openai_compat": openai_compat_provider,
    "openai_responses": openai_responses_provider,
}

#: Wire families :func:`provider_for` knows.
FAMILIES = tuple(_FACTORIES)


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
    factory = _FACTORIES.get(family)
    if factory is None:
        raise ValueError(f"unknown provider family {family!r}; expected one of {FAMILIES}")
    return factory(base_url=base_url, api_key=api_key, **kw)
