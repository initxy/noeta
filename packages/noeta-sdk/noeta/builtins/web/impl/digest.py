"""`webfetch`'s page digester — answer `prompt` against a fetched page.

Claude Code's `WebFetch` does not hand the raw page to the calling model: it
answers the caller's ``prompt`` against the fetched content with an auxiliary
model call and returns only the answer. This module is that half of the tool:
:class:`PageDigester` is the seam, :class:`LLMPageDigester` the production
implementation binding an :class:`~noeta.protocols.messages.LLMProvider` and a
model id at pack-construction time (a tool's ``ToolContext`` carries no LLM
access, so the binding must happen when the pack is built — the same pattern as
the memory recall judge).

Like the judge, the digester bypasses ``RuntimeLLMClient`` by design: a tool
has no ``StepContext``, and the digest call needs no retry or event recording —
the ToolResult that embeds the answer is what gets recorded, so resume replays
the recorded result and never re-digests. The provider call runs on a daemon
thread under a hard wall-clock cap so a wedged provider degrades (the tool
falls back to the raw page rendering) instead of stalling the step forever.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import Optional, Protocol

from noeta.protocols.messages import (
    LLMProvider,
    LLMRequest,
    LLMResponse,
    Message,
    TextBlock,
)
from noeta.protocols.resources import load_markdown


__all__ = [
    "PageDigester",
    "LLMPageDigester",
    "render_digest_prompt",
]


#: Ceiling on the digest answer. Generous — a thorough extraction (an API
#: listing, a changelog) is legitimate output — while still bounding what one
#: fetch can push into the calling model's context.
_DIGEST_MAX_TOKENS = 4096

#: Wall-clock cap on one digest call. Past it the tool falls back to the raw
#: page rendering — a slow provider must degrade the answer, never hang the
#: step.
DEFAULT_DIGEST_TIMEOUT_SECONDS = 60.0

_DIGEST_INSTRUCTIONS = load_markdown(__package__, "webfetch_digest")


class PageDigester(Protocol):
    """``(page, prompt) -> answer``. Raises on failure; the tool degrades."""

    def digest(
        self, *, url: str, title: str, page_markdown: str, prompt: str
    ) -> str: ...


def render_digest_prompt(
    *, url: str, title: str, page_markdown: str, prompt: str
) -> str:
    """The one user message the digest model sees: instructions, request, page.

    The prompt is ephemeral (never recorded — only the returned answer rides
    the ToolResult), so embedding the full page rendering moves no ledger
    bytes.
    """
    return "\n".join(
        [
            _DIGEST_INSTRUCTIONS.strip(),
            "",
            f"Request: {prompt}",
            "",
            f"Page title: {title}",
            f"Page URL: {url}",
            "",
            "Page content (Markdown):",
            page_markdown,
        ]
    )


def _complete_bounded(
    provider: LLMProvider, request: LLMRequest, timeout_seconds: float
) -> Optional[LLMResponse]:
    """One provider call under a wall-clock cap; ``None`` ⇒ timed out.

    The call runs on a daemon I/O thread so an abandoned wait cannot stall the
    step. Abandonment is safe because ``LLMProvider`` is contractually pure:
    the orphan call writes nothing and its eventual result has no consumer. A
    provider exception is re-raised on the calling thread, so the tool's
    degrade-to-raw-render catch keeps owning failures.
    """
    outcome: list[tuple[str, object]] = []
    done = threading.Event()

    def _run() -> None:
        try:
            outcome.append(("ok", provider.complete(request)))
        except BaseException as exc:  # noqa: BLE001 — re-raised on the calling thread
            outcome.append(("err", exc))
        finally:
            done.set()

    worker = threading.Thread(
        target=_run, name="noeta-webfetch-digest", daemon=True
    )
    worker.start()
    if not done.wait(timeout_seconds):
        return None
    kind, value = outcome[0]
    if kind == "err":
        assert isinstance(value, BaseException)
        raise value
    assert isinstance(value, LLMResponse)
    return value


@dataclass
class LLMPageDigester:
    """Bind provider + model into a :class:`PageDigester`.

    ``temperature=0`` because extraction should be as stable as a sampled call
    can be. Which model serves the call is host wiring
    (``Options.webfetch_model``, falling back to the session's main model) —
    resolved before this object is built, so this module never imports the
    providers built-in.
    """

    provider: LLMProvider
    model: str
    timeout_seconds: float = DEFAULT_DIGEST_TIMEOUT_SECONDS

    def digest(
        self, *, url: str, title: str, page_markdown: str, prompt: str
    ) -> str:
        request = LLMRequest(
            model=self.model,
            messages=[
                Message(
                    role="user",
                    content=[
                        TextBlock(
                            text=render_digest_prompt(
                                url=url,
                                title=title,
                                page_markdown=page_markdown,
                                prompt=prompt,
                            )
                        )
                    ],
                )
            ],
            temperature=0.0,
            max_tokens=_DIGEST_MAX_TOKENS,
        )
        response = _complete_bounded(self.provider, request, self.timeout_seconds)
        if response is None:
            raise TimeoutError(
                f"webfetch digest call exceeded {self.timeout_seconds}s"
            )
        return "".join(
            block.text
            for block in response.content
            if isinstance(block, TextBlock)
        ).strip()
