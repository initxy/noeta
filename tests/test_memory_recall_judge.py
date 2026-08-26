"""Recall judge — the semantic fallback behind ``Options.recall_model``.

* Prompt/parse — pure pieces: the judge prompt lists the index (aliases
  included) plus the message; the reply parser is strict against
  hallucinated names, garbage, and over-length picks.
* Provider binding — ``build_recall_judge`` shapes one small-model call
  (pinned model, temperature 0, bounded reply) and degrades EVERY failure
  to a lexical miss, never a failed turn.
* Reminder provider — the judge is consulted only on a lexical MISS, its
  picks ride as tier-2 pointers, and an empty store never spends a call.
* Bounded wait (interrupt-responsiveness D9) — the judge's provider call is
  abort-aware (cancel poll) and wall-clock capped, so a stop pressed during
  recall or a wedged provider degrades to a miss instead of stalling turn
  intake; the abandoned daemon call's result has no consumer.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from noeta.builtins.memory.impl.judge import (
    build_recall_judge,
    parse_judge_reply,
    render_judge_prompt,
)
from noeta.builtins.memory.impl.recall import memory_reminder_provider
from noeta.builtins.memory.impl.store import MemoryStore
from noeta.execution.reminders import RecallView, ResidentActivation
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


class _BlockedProvider:
    """Blocks inside ``complete`` until released — a wedged/slow judge call."""

    def __init__(self) -> None:
        self.release = threading.Event()
        self.entered = threading.Event()
        self.finished = threading.Event()
        self.calls = 0

    def complete(self, request):  # noqa: ANN001 - protocol shape
        self.calls += 1
        self.entered.set()
        assert self.release.wait(timeout=10.0), "test forgot to release"
        self.finished.set()
        return LLMResponse(
            stop_reason="end_turn",
            content=[TextBlock(text='["deploy-process"]')],
        )


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
# Bounded wait — abort poll + wall-clock cap (interrupt-responsiveness D9)
# ---------------------------------------------------------------------------


def test_judge_abort_mid_call_returns_miss_promptly() -> None:
    """A stop flipping the abort predicate while the provider call is blocked
    abandons the wait — recall returns ``()`` with the provider still open."""
    provider = _BlockedProvider()
    flag = threading.Event()
    judge = build_recall_judge(
        provider, "small-model-1", should_abort=flag.is_set
    )

    result: list[tuple[str, ...]] = []
    intake = threading.Thread(
        target=lambda: result.append(judge(_ENTRIES, "怎么上线？"))
    )
    intake.start()
    assert provider.entered.wait(timeout=5.0)
    flag.set()
    intake.join(timeout=5.0)
    assert not intake.is_alive(), "judge must return without the provider"

    assert result == [()]
    # The wait was abandoned, not completed; the orphan's late result has no
    # consumer.
    assert not provider.finished.is_set()
    provider.release.set()
    assert provider.finished.wait(timeout=5.0)


def test_judge_wall_clock_cap_bounds_a_wedged_provider() -> None:
    """No cancel at all: the cap alone turns a wedged provider into a miss —
    turn intake can never be stalled past the cap."""
    provider = _BlockedProvider()
    judge = build_recall_judge(
        provider, "small-model-1", timeout_seconds=0.2
    )

    start = time.monotonic()
    assert judge(_ENTRIES, "怎么上线？") == ()
    assert time.monotonic() - start < 2.0  # the cap, not the 10 s wedge
    assert not provider.finished.is_set()
    provider.release.set()


def test_judge_pre_armed_abort_never_spends_the_call() -> None:
    provider = _BlockedProvider()
    judge = build_recall_judge(
        provider, "small-model-1", should_abort=lambda: True
    )
    assert judge(_ENTRIES, "怎么上线？") == ()
    assert provider.calls == 0


def test_judge_normal_call_unchanged_under_abort_seam() -> None:
    """An armed-but-never-tripped seam changes nothing about the results."""
    provider = _ScriptedProvider(reply='["deploy-process"]')
    judge = build_recall_judge(
        provider, "small-model-1", should_abort=lambda: False
    )
    assert judge(_ENTRIES, "怎么上线？") == ("deploy-process",)
    (request,) = provider.requests
    assert request.model == "small-model-1"


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

    # Lexical hit: the judge is never consulted and tier-1 keeps its body —
    # as a memory-kind resident activation, not a turn.
    (hit,) = provider(_view("how do we deploy?"))
    assert calls == ["怎么上线？"]  # unchanged
    assert isinstance(hit, ResidentActivation)
    assert (hit.kind, hit.name) == ("memory", "deploy-process")
    assert b"Always run make deploy." in hit.body


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


def test_host_wires_judge_abort_to_task_cancellation(tmp_path: Path) -> None:
    """End-to-end D9 wiring: the host binds the judge's abort poll to its
    cancellation registry keyed by the intake ``task_id`` — the same registry
    the ``interrupt`` / ``cancel`` verbs arm — so a stop pressed while the
    judge call is blocked returns the intake reminder pass promptly as a
    miss instead of stalling turn entry."""
    from tests._sdk_session import make_host, make_registry, runner_main_spec

    mem = tmp_path / "memories"
    MemoryStore(root=mem).write(
        "deploy-process",
        "---\ndescription: How we deploy safely\n---\nAlways run make deploy.",
    )
    provider = _BlockedProvider()
    host = make_host(
        make_registry(runner_main_spec("main", memory=True)),
        workspace_dir=tmp_path,
        provider=provider,
        model="stub-model",
        global_memory_dir=mem,
        recall_model="judge-model",
    )
    (intake,) = host.intake_reminder_providers("main", task_id="root-1")

    result: list[tuple] = []
    turn = threading.Thread(
        target=lambda: result.append(intake(_view("怎么上线？")))
    )
    turn.start()
    assert provider.entered.wait(timeout=5.0)  # judge call in flight
    host.request_cancellation("root-1")  # what interrupt/cancel arm
    turn.join(timeout=5.0)
    assert not turn.is_alive(), "intake must return without the provider"

    assert result == [()]  # a stop during recall is just a miss
    assert not provider.finished.is_set()  # abandoned, not completed
    provider.release.set()
