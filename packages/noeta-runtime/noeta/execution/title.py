"""Title internal agent — short session-title generation.

A small host/runtime-side effect, NOT a user-facing preset and NOT a
Subtask (same judgement applies to ``compaction`` /
``memory-retrieval`` / ``permission-judge``: these are host-layer effects
with no Policy, so they do **not** travel the workflow Subtask channel and
do **not** enter the ``noeta.presets`` four-piece set or its golden
fingerprint). It is organised exactly like the compaction summarize
round-trip in :mod:`noeta.policies.react`: a single deterministic
``LLMRequest`` over the conversation, run through the host's plain
``LLMProvider``.

Shape parallels the compaction summarizer:

* :func:`build_title_request` — the title agent's request builder. A
  fixed system instruction over the conversation so far; deterministic
  (same messages → same request), so if a host ever records the call a
  resume rebuilds the same request bytes. Provider-neutral: the request is
  Noeta-shape, no vendor field.
* :func:`generate_title` — call the provider, then deterministically
  shrink the response to ONE short single-line title
  (:func:`clean_title`). The provider is a plain
  :class:`noeta.protocols.messages.LLMProvider`; this module never touches
  the EventLog, Engine, or any runtime primitive.

Why no AgentSpec / preset: a title is a system-side convenience for the
front-end / external callers (a session label), not a delegated unit of
work with its own tool whitelist. Wiring it as a preset would mint an
extra golden fingerprint for zero capability — exactly what it
rejects ("no tool whitelist, not user-facing").
"""

from __future__ import annotations

from noeta.protocols.messages import (
    Block,
    HeaderAwareProvider,
    LLMProvider,
    LLMRequest,
    Message,
    TextBlock,
)


__all__ = [
    "DEFAULT_TITLE_MAX_CHARS",
    "TITLE_SYSTEM_PROMPT",
    "build_title_request",
    "clean_title",
    "generate_title",
]


#: Hard cap on the emitted title length (characters). A session label is a
#: glanceable string, not a sentence; the model is *asked* to stay terse
#: (prompt) and the deterministic post-check (:func:`clean_title`) is the
#: actual guarantee — it truncates anything longer at a word boundary.
DEFAULT_TITLE_MAX_CHARS = 60


#: The title agent's fixed system instruction. Mirrors the compaction
#: summarizer's "single fixed instruction" discipline so the request is
#: deterministic. Deliberately demands a bare title (no quotes, no
#: trailing punctuation, one line) so :func:`clean_title` rarely has to
#: do more than trim.
TITLE_SYSTEM_PROMPT = (
    "You generate a short, descriptive title for a chat session, for use "
    "as a label in a session list.\n"
    "Read the conversation and output ONLY the title — no quotes, no "
    "markdown, no trailing punctuation, on a single line. Keep it under "
    f"{DEFAULT_TITLE_MAX_CHARS} characters and prefer a noun phrase that "
    "names the task or topic (e.g. \"Fix login redirect bug\")."
)


def _text_of(content: list[Block]) -> str:
    """Concatenate the ``TextBlock`` texts of one turn (newline-joined).

    Non-text blocks (images, tool-use/result) carry no title signal and
    are skipped — same projection rule memory recall uses for its match
    key (D5).
    """
    return "\n".join(b.text for b in content if isinstance(b, TextBlock))


def build_title_request(messages: list[Message], *, model: str) -> LLMRequest:
    """Build the deterministic title round-trip request.

    The conversation is passed through as the message history; the fixed
    :data:`TITLE_SYSTEM_PROMPT` rides ``system`` (orthogonal to history,
    like every other Noeta request). Empty / non-text turns are kept as-is
    in the history — the provider sees the real conversation shape. Pure
    over ``(messages, model)``: same inputs → same request, no clock /
    randomness / network, so a resume rebuilds a recorded call byte-equal.
    """
    return LLMRequest(
        model=model,
        messages=list(messages),
        tools=[],
        system=Message(
            role="system",
            content=[TextBlock(text=TITLE_SYSTEM_PROMPT)],
        ),
    )


def clean_title(raw: str, *, max_chars: int = DEFAULT_TITLE_MAX_CHARS) -> str:
    """Deterministically shrink a model response to one short title line.

    The model is *asked* for a bare single-line title but cannot be
    trusted to, so this is the actual guarantee:

    * take the first non-empty line (drop any extra lines / reasoning);
    * strip surrounding whitespace and a single layer of wrapping quotes
      (``"`` / ``'`` / ``“”`` / ``‘’``) and leading
      markdown bullets / ``#`` heading marks;
    * if longer than ``max_chars``, cut at the last word boundary within
      the budget (falling back to a hard cut when a single token is
      already too long), with no trailing partial word.

    Pure + deterministic: same input → same output, no IO. An all-empty
    response yields ``""`` (the caller decides the fallback label).
    """
    first = ""
    for line in raw.splitlines():
        stripped = line.strip()
        if stripped:
            first = stripped
            break
    if not first:
        return ""
    # Strip leading markdown bullet / heading marks, then a single layer
    # of matching wrapping quotes.
    first = first.lstrip("#").lstrip("-*").strip()
    pairs = {'"': '"', "'": "'", "“": "”", "‘": "’"}
    if len(first) >= 2 and first[0] in pairs and first[-1] == pairs[first[0]]:
        first = first[1:-1].strip()
    if len(first) <= max_chars:
        return first
    cut = first[:max_chars]
    if " " in cut:
        cut = cut[: cut.rindex(" ")]
    return cut.strip()


def generate_title(
    provider: LLMProvider,
    messages: list[Message],
    *,
    model: str,
    max_chars: int = DEFAULT_TITLE_MAX_CHARS,
    provider_headers: dict[str, str] | None = None,
) -> str:
    """Run the title agent over a conversation and return a short title.

    Builds the deterministic request (:func:`build_title_request`), calls
    the host-supplied ``provider`` (a plain
    :class:`noeta.protocols.messages.LLMProvider`), and shrinks the
    response with :func:`clean_title`. An empty conversation or an empty
    model response yields ``""`` — the caller owns the fallback label
    (e.g. the session id), so this stays a pure transform with no opinion
    on UI defaults.
    """
    if not messages:
        return ""
    request = build_title_request(messages, model=model)
    if provider_headers is not None and isinstance(provider, HeaderAwareProvider):
        response = provider.complete_with_headers(request, provider_headers)
    else:
        response = provider.complete(request)
    return clean_title(_text_of(response.content), max_chars=max_chars)
