"""Recall judge — the semantic fallback behind ``Options.recall_model``.

* Prompt/parse — pure pieces: the judge prompt lists the index (aliases
  included) plus the message; the reply parser is strict against
  hallucinated names, garbage, and over-length picks.
* Provider binding — ``build_recall_judge`` shapes one small-model call
  (pinned model, temperature 0, bounded reply) and degrades EVERY failure
  to a lexical miss, never a failed turn.
* Reminder provider — the judge is consulted only on a lexical MISS, its
  picks ride as tier-2 pointers, and an empty store never spends a call.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from noeta.builtins.memory.impl.judge import (
    build_recall_judge,
    parse_judge_reply,
    render_judge_prompt,
)
from noeta.builtins.memory.impl.recall import memory_reminder_provider
from noeta.builtins.memory.impl.store import MemoryStore
from noeta.execution.reminders import RecallView
from noeta.protocols.messages import LLMResponse, TextBlock


_ENTRIES = (
    ("deploy-process", "How we deploy safely", "procedural", "deploy, 部署"),
    ("naming-rules", "Module naming conventions", "", ""),
)


def _view(text: str) -> RecallView:
    return RecallView(
        task_id="t-1",
        message=(TextBlock(text=text),),
        task_state=None,
        workspace_path=None,
    )


@dataclass
class _ScriptedProvider:
    """LLMProvider stub: replies with a fixed string, records requests."""

    reply: str
    requests: list = field(default_factory=list)

    def complete(self, request):  # noqa: ANN001 - protocol shape
        self.requests.append(request)
        return LLMResponse(
            stop_reason="end_turn", content=[TextBlock(text=self.reply)]
        )


@dataclass
class _ExplodingProvider:
    def complete(self, request):  # noqa: ANN001 - protocol shape
        raise RuntimeError("provider down")


# ---------------------------------------------------------------------------
# Prompt + parse — pure pieces
# ---------------------------------------------------------------------------


def test_prompt_lists_index_with_aliases_and_message() -> None:
    prompt = render_judge_prompt(_ENTRIES, "怎么上线？")
    assert "JSON array" in prompt  # the reply contract is stated
    assert "- deploy-process (procedural): How we deploy safely" in prompt
    assert "[aliases: deploy, 部署]" in prompt  # keywords ride into the prompt
    assert "- naming-rules: Module naming conventions" in prompt
    assert prompt.rstrip().endswith("怎么上线？")


def test_parse_keeps_known_names_in_judge_order() -> None:
    reply = '["naming-rules", "deploy-process"]'
    assert parse_judge_reply(reply, _ENTRIES) == (
        "naming-rules",
        "deploy-process",
    )


def test_parse_filters_hallucinated_dupes_and_non_strings() -> None:
    reply = '["ghost-memory", "deploy-process", "deploy-process", 7, null]'
    assert parse_judge_reply(reply, _ENTRIES) == ("deploy-process",)


def test_parse_survives_prose_wrapped_json_and_garbage() -> None:
    wrapped = 'Sure! The relevant ones are ["deploy-process"] — hope it helps.'
    assert parse_judge_reply(wrapped, _ENTRIES) == ("deploy-process",)
    assert parse_judge_reply("no array here", _ENTRIES) == ()
    assert parse_judge_reply('{"not": "a list"}', _ENTRIES) == ()
    assert parse_judge_reply("[not json", _ENTRIES) == ()


def test_parse_caps_at_recall_limit() -> None:
    entries = tuple((f"m-{i}", "s", "", "") for i in range(10))
    reply = "[" + ", ".join(f'"m-{i}"' for i in range(10)) + "]"
    assert len(parse_judge_reply(reply, entries)) == 5  # DEFAULT_RECALL_MAX_HITS


# ---------------------------------------------------------------------------
# build_recall_judge — the bound small-model call
# ---------------------------------------------------------------------------


def test_judge_call_shape_and_selection() -> None:
    provider = _ScriptedProvider(reply='["deploy-process"]')
    judge = build_recall_judge(provider, "small-model-1")
    assert judge(_ENTRIES, "怎么上线？") == ("deploy-process",)
    (request,) = provider.requests
    assert request.model == "small-model-1"
    assert request.temperature == 0.0
    assert request.max_tokens is not None  # bounded reply
    assert request.tools == []  # a selector, not an agent


def test_judge_empty_entries_skips_the_call() -> None:
    provider = _ScriptedProvider(reply='["anything"]')
    judge = build_recall_judge(provider, "small-model-1")
    assert judge((), "hello") == ()
    assert provider.requests == []  # no entries, no spend


def test_judge_provider_failure_degrades_to_miss() -> None:
    judge = build_recall_judge(_ExplodingProvider(), "small-model-1")
    assert judge(_ENTRIES, "怎么上线？") == ()  # never raises


# ---------------------------------------------------------------------------
# memory_reminder_provider(judge=...) — miss-only, pointers-only
# ---------------------------------------------------------------------------


def _store_with_english_memory(tmp_path: Path) -> MemoryStore:
    store = MemoryStore(root=tmp_path / "memories")
    store.write(
        "deploy-process",
        "---\ndescription: How we deploy safely\n---\nAlways run make deploy.",
    )
    return store


def test_judge_fires_only_on_lexical_miss(tmp_path: Path) -> None:
    store = _store_with_english_memory(tmp_path)
    calls: list[str] = []

    def judge(entries, text):  # noqa: ANN001 - RecallJudge shape
        calls.append(text)
        return ("deploy-process",)

    provider = memory_reminder_provider(store, judge=judge)

    # Chinese message, no keywords in the store: lexical miss → judge picks,
    # and the pick rides as a POINTER (summary + memory_read affordance),
    # never a body.
    (reminder,) = provider(_view("怎么上线？"))
    assert calls == ["怎么上线？"]
    assert reminder.origin == "memory"
    assert "- deploy-process: How we deploy safely" in reminder.text
    assert "memory_read" in reminder.text
    assert "make deploy" not in reminder.text  # body not spent on a guess

    # Lexical hit: the judge is never consulted and tier-1 keeps its body.
    (hit,) = provider(_view("how do we deploy?"))
    assert calls == ["怎么上线？"]  # unchanged
    assert "Always run make deploy." in hit.text


def test_judge_returning_nothing_stays_a_miss(tmp_path: Path) -> None:
    store = _store_with_english_memory(tmp_path)
    provider = memory_reminder_provider(store, judge=lambda e, t: ())
    assert provider(_view("完全无关的话题")) == ()


def test_empty_store_never_consults_the_judge(tmp_path: Path) -> None:
    calls: list[str] = []

    def judge(entries, text):  # noqa: ANN001 - RecallJudge shape
        calls.append(text)
        return ()

    store = MemoryStore(root=tmp_path / "empty")
    provider = memory_reminder_provider(store, judge=judge)
    assert provider(_view("anything at all")) == ()
    assert calls == []


def test_no_judge_keeps_v1_bytes(tmp_path: Path) -> None:
    # judge=None (the default) is byte-identical to the pre-judge provider:
    # miss stays a miss.
    store = _store_with_english_memory(tmp_path)
    provider = memory_reminder_provider(store)
    assert provider(_view("怎么上线？")) == ()


# ---------------------------------------------------------------------------
# Host wiring — Options.recall_model → SdkHost → intake provider
# ---------------------------------------------------------------------------


def test_host_recall_model_binds_the_judge(tmp_path: Path) -> None:
    """The wiring chain end-to-end: a host with ``recall_model`` set hands out
    an intake provider whose lexical miss consults the default provider with
    that model; without ``recall_model`` the same miss stays silent and the
    provider is never called."""
    from noeta.testing.fake_llm import FakeLLMProvider
    from tests._sdk_session import make_host, make_registry, runner_main_spec

    mem = tmp_path / "memories"
    MemoryStore(root=mem).write(
        "deploy-process",
        "---\ndescription: How we deploy safely\n---\nAlways run make deploy.",
    )
    judge_reply = LLMResponse(
        stop_reason="end_turn",
        content=[TextBlock(text='["deploy-process"]')],
    )

    llm = FakeLLMProvider(responses=[judge_reply])
    host = make_host(
        make_registry(runner_main_spec("main", memory=True)),
        workspace_dir=tmp_path,
        provider=llm,
        model="stub-model",
        global_memory_dir=mem,
        recall_model="judge-model",
    )
    (provider,) = host.intake_reminder_providers("main")
    (reminder,) = provider(_view("怎么上线？"))
    assert reminder.origin == "memory"
    assert "- deploy-process: How we deploy safely" in reminder.text
    (request,) = llm.received_requests
    assert request.model == "judge-model"  # unknown alias rides through as-is

    silent_llm = FakeLLMProvider(responses=[])
    bare = make_host(
        make_registry(runner_main_spec("main", memory=True)),
        workspace_dir=tmp_path,
        provider=silent_llm,
        model="stub-model",
        global_memory_dir=mem,
    )
    (lexical_only,) = bare.intake_reminder_providers("main")
    assert lexical_only(_view("怎么上线？")) == ()
    assert silent_llm.received_requests == []
