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
miss. Auto-recall is a nice-to-have and must never take a turn down.
"""

from __future__ import annotations

import json
from typing import Callable

from noeta.builtins.memory.impl.matching import (
    DEFAULT_RECALL_MAX_HITS,
    MemoryEntries,
)
from noeta.protocols.messages import (
    LLMProvider,
    LLMRequest,
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


def build_recall_judge(provider: LLMProvider, model: str) -> RecallJudge:
    """Bind provider + model into a :data:`RecallJudge`.

    ``temperature=0`` because selection should be as stable as a sampled
    call can be. The catch-all is deliberate and total (see module note):
    a judge failure IS a lexical miss, never a failed turn.
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
            response = provider.complete(request)
            reply = "".join(
                block.text
                for block in response.content
                if isinstance(block, TextBlock)
            )
            return parse_judge_reply(reply, entries)
        except Exception:
            return ()

    return judge
