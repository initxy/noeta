"""Memory recall judge — the semantic fallback for lexical auto-recall.

The lexical matcher is deterministic and free but literal: a message that
*means* "deploy" without saying it recalls nothing. When a host sets
``Options.recall_model``, a lexical MISS at turn intake is retried through
one small-model call — the judge reads the incoming message plus the index
lines and picks the memories worth surfacing. Its picks ride as tier-2
pointers (a judge is a guess, and a guess is worth a pointer, not a body),
and the formatted reminder is RECORDED like any other recall, so
resume/replay folds the judged recall back without re-invoking the model.

Placement note: the judge sits at the ONE spot on the recall path where no
model is otherwise present — turn intake is runtime plumbing. Mid-process
retrieval needs no judge: the main model is already in the loop there,
reading the index itself and reformulating ``memory_search`` queries.

Degradation is total by design: any provider error, malformed reply, or
hallucinated name yields ``()`` — the turn proceeds exactly as a lexical
miss. Auto-recall is a nice-to-have and must never take a turn down. The
same rule bounds the call in TIME: the provider call runs under an
abort-aware, wall-clock-capped wait (:func:`_complete_bounded`), so a human
stop pressed during recall — or a wedged provider — is also just a miss,
never a stalled turn entry.
"""

from __future__ import annotations

import json
import threading
import time
from typing import Any, Callable, Optional

from noeta.builtins.memory.impl.matching import (
    DEFAULT_RECALL_MAX_HITS,
    MemoryEntries,
)
from noeta.protocols.messages import (
    LLMProvider,
    LLMRequest,
    LLMResponse,
    Message,
    TextBlock,
)
from noeta.protocols.resources import load_markdown


__all__ = [
    "RecallJudge",
    "build_recall_judge",
    "parse_judge_reply",
    "render_judge_prompt",
]


#: A judge: ``(entries, user text) -> names to surface``. Bound to a live
#: provider + model by :func:`build_recall_judge`; a stub suffices in tests.
RecallJudge = Callable[[MemoryEntries, str], tuple[str, ...]]

#: The judge's reply is a JSON array of at most a handful of slugs.
_JUDGE_MAX_TOKENS = 200

#: Poll interval of the bounded wait around the judge's provider call.
_JUDGE_POLL_SECONDS = 0.05

#: Default wall-clock cap on one judge call. The judge runs on the turn-intake
#: path (before the goal even enters the ledger), so a wedged provider here
#: would stall the whole turn entry — the cap turns that into a lexical miss.
DEFAULT_JUDGE_TIMEOUT_SECONDS = 10.0

_JUDGE_INSTRUCTIONS = load_markdown(__package__, "recall_judge")


def render_judge_prompt(entries: MemoryEntries, text: str) -> str:
    """The one user message the judge sees: instructions, index, message.

    Unlike the rendered index resident, the prompt DOES include keywords —
    they are curator-written aliases, exactly the cross-language hints a
    selector benefits from, and this prompt is ephemeral (never recorded),
    so including them moves no ledger bytes.
    """
    lines = [_JUDGE_INSTRUCTIONS.strip(), "", "Memory index:"]
    for name, summary, mem_type, keywords in entries:
        label = f"{name} ({mem_type})" if mem_type else name
        line = f"- {label}: {summary}" if summary else f"- {label}"
        if keywords:
            line += f" [aliases: {keywords}]"
        lines.append(line)
    lines += ["", "User message:", text]
    return "\n".join(lines)


def parse_judge_reply(reply: str, entries: MemoryEntries) -> tuple[str, ...]:
    """Extract the judged names — strict against everything but honesty.

    Takes the first ``[...]`` span so prose-wrapped JSON still parses;
    keeps only strings that name an EXISTING entry (a hallucinated slug
    must not become a pointer to nothing), dedupes preserving the judge's
    order, and caps at the recall limit. Anything unparseable is ``()``.
    """
    start = reply.find("[")
    end = reply.rfind("]")
    if start < 0 or end <= start:
        return ()
    try:
        picked = json.loads(reply[start : end + 1])
    except ValueError:
        return ()
    if not isinstance(picked, list):
        return ()
    known = {name for name, _s, _t, _k in entries}
    out: list[str] = []
    for item in picked:
        if isinstance(item, str) and item in known and item not in out:
            out.append(item)
        if len(out) >= DEFAULT_RECALL_MAX_HITS:
            break
    return tuple(out)


def _complete_bounded(
    provider: LLMProvider,
    request: LLMRequest,
    should_abort: Optional[Callable[[], bool]],
    timeout_seconds: float,
) -> Optional[LLMResponse]:
    """One provider call under a bounded, abort-aware wait; ``None`` ⇒ give up.

    The judge bypasses ``RuntimeLLMClient`` by design (no retry, no
    recording), so it inherits none of its abandonable wait — this is the
    local equivalent: the call runs on a daemon I/O thread while the intake
    thread polls ``should_abort`` (the host's cancellation registry, armed by
    ``interrupt`` / ``cancel``) between short waits, under a hard wall-clock
    cap so a wedged provider can never stall turn intake. Abandonment is safe
    because :class:`~noeta.protocols.messages.LLMProvider` is contractually
    pure: the orphan call writes nothing and its eventual result simply has
    no consumer. A provider exception is re-raised on the intake thread, so
    the caller's total degrade-to-miss catch keeps owning failures.
    """
    if should_abort is not None and should_abort():
        return None
    outcome: list[tuple[str, Any]] = []
    done = threading.Event()

    def _run() -> None:
        try:
            outcome.append(("ok", provider.complete(request)))
        except BaseException as exc:  # noqa: BLE001 — re-raised on the intake thread
            outcome.append(("err", exc))
        finally:
            done.set()

    worker = threading.Thread(
        target=_run, name="noeta-recall-judge", daemon=True
    )
    worker.start()
    deadline = time.monotonic() + timeout_seconds
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return None
        if done.wait(min(_JUDGE_POLL_SECONDS, remaining)):
            break
        if should_abort is not None and should_abort():
            return None
    kind, value = outcome[0]
    if kind == "err":
        raise value
    response: LLMResponse = value
    return response


def build_recall_judge(
    provider: LLMProvider,
    model: str,
    *,
    should_abort: Optional[Callable[[], bool]] = None,
    timeout_seconds: float = DEFAULT_JUDGE_TIMEOUT_SECONDS,
) -> RecallJudge:
    """Bind provider + model into a :data:`RecallJudge`.

    ``temperature=0`` because selection should be as stable as a sampled
    call can be. The catch-all is deliberate and total (see module note):
    a judge failure IS a lexical miss, never a failed turn.

    ``should_abort`` (host-wired to the cancellation registry keyed by the
    intake task) and ``timeout_seconds`` bound the provider call — a stop
    pressed during recall, or a wedged provider, degrades to the SAME lexical
    miss instead of stalling turn entry (see :func:`_complete_bounded`).
    """

    def judge(entries: MemoryEntries, text: str) -> tuple[str, ...]:
        if not entries:
            return ()
        request = LLMRequest(
            model=model,
            messages=[
                Message(
                    role="user",
                    content=[
                        TextBlock(text=render_judge_prompt(entries, text))
                    ],
                )
            ],
            temperature=0.0,
            max_tokens=_JUDGE_MAX_TOKENS,
        )
        try:
            response = _complete_bounded(
                provider, request, should_abort, timeout_seconds
            )
            if response is None:
                return ()
            reply = "".join(
                block.text
                for block in response.content
                if isinstance(block, TextBlock)
            )
            return parse_judge_reply(reply, entries)
        except Exception:
            return ()

    return judge
