"""Safety/permission constraints survive compaction verbatim.

The compaction summary is **model-generated** (``ReActPolicy._compaction_decision``
runs a summarize round-trip through the recorded ``RuntimeLLMClient``). Left to
its own devices, a model will happily paraphrase or drop a "do not touch X"
instruction when it collapses a long prefix into a concise note — and once that
constraint is paraphrased away, the safety rule it encoded silently stops
binding the rest of the session.

The tool/agent catalog makes the ``compaction`` agent keep those constraints **verbatim**.
Two layers enforce it:

1. the summarize PROMPT carries a hard rule telling the model to copy
   safety/permission directives word-for-word (best effort, model-facing);
2. a deterministic POST-CHECK re-injects any detected constraint line that did
   not survive verbatim into the produced summary, so the invariant holds even
   when the model ignores the rule. The post-check is pure over
   ``(history, summary)`` so resume re-derives the same summary bytes.

These tests pin invariant (2): a constraint line present in the collapsed prefix
appears character-for-character in the resulting ``CompactionRequestedDecision
.summary``.
"""

from __future__ import annotations

from typing import Any

from noeta.policies.react import (
    ReActPolicy,
    extract_safety_constraints,
    enforce_verbatim_constraints,
)
from noeta.protocols.decisions import CompactionRequestedDecision
from noeta.protocols.messages import LLMResponse, Message, TextBlock
from noeta.protocols.step_context import StepContext
from noeta.runtime.llm import RuntimeLLMClient
from noeta.storage.memory import InMemoryContentStore, InMemoryEventLog
from noeta.testing.composer import fake_view
from noeta.testing.fake_llm import FakeLLMProvider
from noeta.tools.fake import FakeTool


def _ctx() -> StepContext:
    return StepContext(task_id="t-1", lease_id="l-1", trace_id="tr-1")


# ---------------------------------------------------------------------------
# unit: constraint detection
# ---------------------------------------------------------------------------


def test_extract_detects_english_directives() -> None:
    msgs = [
        Message(role="user", content=[TextBlock(text="Please help refactor.")]),
        Message(
            role="user",
            content=[TextBlock(text="Do not touch config/secrets.yaml ever.")],
        ),
        Message(
            role="user",
            content=[TextBlock(text="Never edit the vendored lib/ directory.")],
        ),
    ]
    found = extract_safety_constraints(msgs)
    assert "Do not touch config/secrets.yaml ever." in found
    assert "Never edit the vendored lib/ directory." in found
    # The benign line is not treated as a constraint.
    assert all("refactor" not in c for c in found)


def test_extract_detects_chinese_directives() -> None:
    msgs = [
        Message(role="user", content=[TextBlock(text="禁止访问 /etc/shadow 这个文件。")]),
        Message(role="user", content=[TextBlock(text="不得修改 vendor 目录下的任何东西。")]),
        Message(role="user", content=[TextBlock(text="The weather is nice today.")]),
    ]
    found = extract_safety_constraints(msgs)
    assert "禁止访问 /etc/shadow 这个文件。" in found
    assert "不得修改 vendor 目录下的任何东西。" in found
    # The benign line is not treated as a constraint.
    assert all("weather" not in c for c in found)


def test_extract_is_deterministic_and_dedups() -> None:
    line = "Do not touch the database."
    msgs = [
        Message(role="user", content=[TextBlock(text=line)]),
        Message(role="user", content=[TextBlock(text=line)]),
    ]
    found = extract_safety_constraints(msgs)
    assert found.count(line) == 1


def test_enforce_passes_through_when_already_verbatim() -> None:
    constraints = ["Do not touch secrets.yaml."]
    summary = "User asked for a refactor. Do not touch secrets.yaml. Goals open."
    out = enforce_verbatim_constraints(summary, constraints)
    assert out == summary  # nothing missing → no rewrite


def test_enforce_reinjects_dropped_constraint_verbatim() -> None:
    constraints = ["Do not touch config/secrets.yaml ever."]
    # The model paraphrased the rule away.
    summary = "User asked to refactor; be careful with secret files."
    out = enforce_verbatim_constraints(summary, constraints)
    assert "Do not touch config/secrets.yaml ever." in out
    # The original paraphrase is preserved too — we ADD, never replace.
    assert "be careful with secret files." in out


def test_enforce_reinjects_chinese_constraint_verbatim() -> None:
    constraints = ["禁止访问 /etc/shadow 这个文件。"]
    summary = "User wants a refactor; be careful with sensitive files."
    out = enforce_verbatim_constraints(summary, constraints)
    assert "禁止访问 /etc/shadow 这个文件。" in out


# ---------------------------------------------------------------------------
# integration: the policy's compaction decision carries the constraint verbatim
# ---------------------------------------------------------------------------


def _big_view_with_constraint(constraint: str, n: int = 40):
    msgs: list[Message] = [
        Message(role="user", content=[TextBlock(text=constraint)]),
    ]
    msgs += [
        Message(role="user", content=[TextBlock(text="x" * 200)])
        for _ in range(n)
    ]
    return fake_view(msgs)


def _policy(responses: list[LLMResponse]) -> tuple[ReActPolicy, FakeLLMProvider]:
    provider = FakeLLMProvider(responses=responses)
    client = RuntimeLLMClient(
        provider=provider,
        event_log=InMemoryEventLog(),
        content_store=InMemoryContentStore(),
    )
    policy = ReActPolicy(
        llm=client,
        tools={"echo": FakeTool(name="echo", script={("hi",): "ok"})},
        system_prompt="sys",
        model="gpt-4o",
        context_window=2000,
        max_output_tokens=500,
        compaction_buffer=100,
        tail_token_budget=200,
        composer_version="three_segment.v3",
    )
    return policy, provider


def test_summarize_prompt_carries_verbatim_rule() -> None:
    """The model-facing summarize prompt must instruct verbatim preservation of
    safety/permission directives."""
    constraint = "Do not touch config/secrets.yaml ever."
    paraphrase_resp = LLMResponse(
        stop_reason="end_turn",
        content=[TextBlock(text="condensed summary, secrets handled carefully")],
    )
    policy, provider = _policy([paraphrase_resp])
    policy.decide(_ctx(), _big_view_with_constraint(constraint))
    # The summarize round-trip is the single recorded LLM call.
    assert len(provider.received_requests) == 1
    system = provider.received_requests[0].system
    assert system is not None
    system_text = "".join(
        b.text for b in system.content if isinstance(b, TextBlock)
    ).lower()
    assert "verbatim" in system_text
    assert "safety" in system_text or "permission" in system_text


def _summarize_system_text(policy: ReActPolicy, provider: FakeLLMProvider,
                           view: Any) -> str:
    """Run a compaction and return the lower-cased summarize system prompt."""
    policy.decide(_ctx(), view)
    assert len(provider.received_requests) == 1
    system = provider.received_requests[0].system
    assert system is not None
    return "".join(
        b.text for b in system.content if isinstance(b, TextBlock)
    )


def test_summarize_prompt_uses_durable_sections() -> None:
    """The summarize prompt is organized into the
    durable-distillation sections — it adopts Claude Code's structure but trims it to a
    durable subset. All seven adopted section headings must be present."""
    resp = LLMResponse(
        stop_reason="end_turn", content=[TextBlock(text="note")]
    )
    policy, provider = _policy([resp])
    text = _summarize_system_text(policy, provider, _big_view_with_constraint(
        "Do not touch x.y"
    ))
    for section in (
        "Primary Request & Intent",
        "Key Technical Concepts",
        "Files & Code",
        "Errors & Fixes",
        "All user messages",
        "Pending Tasks",
        "Decisions & Constraints",
    ):
        assert section in text, f"missing section: {section}"


def test_summarize_prompt_drops_current_work_and_next_step() -> None:
    """Current Work / Next Step are DROPPED — the latest state is
    kept verbatim in the protected tail (D3), so re-narrating it from an
    estimate would be wasteful and could disagree with the verbatim tail."""
    resp = LLMResponse(
        stop_reason="end_turn", content=[TextBlock(text="note")]
    )
    policy, provider = _policy([resp])
    text = _summarize_system_text(policy, provider, _big_view_with_constraint(
        "Do not touch x.y"
    )).lower()
    assert "current work" not in text
    assert "next step" not in text


def test_summarize_prompt_files_section_is_path_list_only() -> None:
    """The Files & Code section asks for RELEVANT FILE PATHS only,
    not file bodies (re-reading disk breaks determinism; a stored snapshot would
    be stale for an actively-edited file). The model is told to re-read with the
    read tool when it needs the current version."""
    resp = LLMResponse(
        stop_reason="end_turn", content=[TextBlock(text="note")]
    )
    policy, provider = _policy([resp])
    text = _summarize_system_text(policy, provider, _big_view_with_constraint(
        "Do not touch x.y"
    ))
    low = text.lower()
    assert "path" in low                          # path list, not contents
    assert "read" in low                          # re-read with the read tool
    assert "do not copy file" in low              # explicit: no bodies inlined


def test_summarize_prompt_is_provider_neutral() -> None:
    """Red line: the summarize prompt names no vendor and no
    vendor-specific mechanism — it must work for any provider."""
    resp = LLMResponse(
        stop_reason="end_turn", content=[TextBlock(text="note")]
    )
    policy, provider = _policy([resp])
    text = _summarize_system_text(policy, provider, _big_view_with_constraint(
        "Do not touch x.y"
    )).lower()
    for vendor in ("anthropic", "claude", "openai", "gpt", "gemini"):
        assert vendor not in text, f"vendor leaked into prompt: {vendor}"


def test_constraint_survives_even_if_model_paraphrases_it_away() -> None:
    """End-to-end invariant: the constraint text appears character-for-character
    in the decision summary even when the model dropped/paraphrased it."""
    constraint = "Do not touch config/secrets.yaml ever."
    # Model produced a summary that does NOT contain the constraint verbatim.
    paraphrase_resp = LLMResponse(
        stop_reason="end_turn",
        content=[TextBlock(text="User wants a refactor; be careful with secrets.")],
    )
    policy, _ = _policy([paraphrase_resp])
    decision = policy.decide(_ctx(), _big_view_with_constraint(constraint))
    assert isinstance(decision, CompactionRequestedDecision)
    assert constraint in decision.summary


def test_constraint_kept_when_model_already_preserved_it() -> None:
    """When the model already kept the constraint verbatim, the summary is not
    double-injected — the constraint appears exactly once."""
    constraint = "Never edit the vendored lib/ directory."
    good_resp = LLMResponse(
        stop_reason="end_turn",
        content=[
            TextBlock(
                text=(
                    "User asked for a refactor. "
                    "Never edit the vendored lib/ directory. Threads open."
                )
            )
        ],
    )
    policy, _ = _policy([good_resp])
    decision = policy.decide(_ctx(), _big_view_with_constraint(constraint))
    assert isinstance(decision, CompactionRequestedDecision)
    assert decision.summary.count(constraint) == 1


def test_no_constraints_leaves_summary_untouched() -> None:
    """A history with no safety/permission directives produces the model summary
    verbatim (no spurious appended block)."""
    summary_text = "condensed summary of the conversation"
    resp = LLMResponse(
        stop_reason="end_turn", content=[TextBlock(text=summary_text)]
    )
    policy, _ = _policy([resp])
    msgs = [
        Message(role="user", content=[TextBlock(text="x" * 200)])
        for _ in range(40)
    ]
    decision = policy.decide(_ctx(), fake_view(msgs))
    assert isinstance(decision, CompactionRequestedDecision)
    assert decision.summary == summary_text
