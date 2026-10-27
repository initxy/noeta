"""The 0.6 engine: run one scenario trial against a live model and collect what scoring reads.

Each trial is a fresh one-shot ``Client`` over the official ``main`` recipe
(:func:`noeta.presets.main_options`) in its own temporary workspace, with real
writes and an unrestricted shell — the shape a coding host runs, minus the
human. Scoring reads the recorded stream through the public message view, so a
scenario never parses the ledger itself.
"""

from __future__ import annotations

import dataclasses
import tempfile
import threading
from pathlib import Path
from typing import Any, Callable, Optional

from noeta.presets import main_options
from noeta.sdk import (
    AssistantMessage,
    Client,
    HostConfig,
    InjectedMessage,
    Options,
    ToolResultView,
    ToolUse,
    UserMessage,
    query,
)
from noeta.sdk.providers import find_spec

from tests import _live_env

#: An uncatalogued model compacts against this output ceiling (the catalog's
#: unknown-model fallback); a window under ceiling + buffer is refused.
_UNKNOWN_MODEL_MAX_OUTPUT_TOKENS = 16_384
_COMPACTION_BUFFER_TOKENS = 2_000
#: Usable history window for the compaction scenario: small enough that a
#: handful of file reads overflows it, large enough to hold main's fixed prefix
#: (~10K tokens) plus a summary.
_SMALL_USABLE_WINDOW = 24_000

#: Bounds a runaway loop well above what any scenario needs.
_MAX_STEPS_PER_TURN = 60


PROVIDERS: dict[str, Callable[[], Any]] = {
    "anthropic": _live_env.build_anthropic_provider,
    "openai_compat": _live_env.build_openai_compat_provider,
    "responses": _live_env.build_responses_provider,
}


from evals.core import (  # noqa: E402 — re-exported for older imports
    SHELL_READ_SEARCH,
    TRIAL_TIMEOUT_S,
    EvalConfig,
    Scenario,
    Trace,
    cleanup,
    mentions,
    path_arg,
    time_trial,
)

__all__ = [
    "PROVIDERS", "SHELL_READ_SEARCH", "TRIAL_TIMEOUT_S", "EvalConfig", "Judge", "Scenario",
    "Trace", "cleanup", "mentions", "path_arg", "run_trial", "time_trial",
]


class Judge:
    """A yes/no grader for what no deterministic check can see."""

    _SYSTEM = (
        "You grade one transcript excerpt against one yes/no question. Reply "
        "with exactly YES or NO on the first line, then one short sentence of "
        "reason. Judge only what the text says, not what it should have said."
    )

    def __init__(self, cfg: EvalConfig) -> None:
        self._cfg = cfg
        self._lock = threading.Lock()
        self.input_tokens = 0
        self.output_tokens = 0
        self.requests = 0

    def ask(self, question: str, text: str) -> bool:
        goal = f"Question: {question}\n\n<excerpt>\n{text}\n</excerpt>"
        result = query(
            Options(system_prompt=self._SYSTEM, plugins=()),
            goal,
            provider=PROVIDERS[self._cfg.provider](),
            model=self._cfg.model,
            workspace_dir=Path(tempfile.gettempdir()),
        )
        usage = result.usage()
        with self._lock:
            self.input_tokens += usage.input_tokens
            self.output_tokens += usage.output_tokens
            self.requests += usage.requests
        return str(result.answer()).strip().upper().startswith("YES")


def _batches(items: list[Any]) -> list[list[ToolUse]]:
    """Group tool calls by the response that issued them.

    One assistant message projects as its text then its tool uses, and its
    results follow; anything from the user/tool side closes the group.
    """
    out: list[list[ToolUse]] = []
    cur: list[ToolUse] = []
    for it in items:
        if isinstance(it, ToolUse):
            cur.append(it)
        elif isinstance(it, (ToolResultView, UserMessage, InjectedMessage)):
            if cur:
                out.append(cur)
                cur = []
    if cur:
        out.append(cur)
    return out


def _host_config(cfg: EvalConfig, root: Path, small_window: bool) -> HostConfig:
    window: Optional[int] = None
    if small_window:
        spec = find_spec(cfg.model)
        ceiling = spec.max_output_tokens if spec else _UNKNOWN_MODEL_MAX_OUTPUT_TOKENS
        window = ceiling + _COMPACTION_BUFFER_TOKENS + _SMALL_USABLE_WINDOW
    return HostConfig(
        write_mode="apply",
        memory_dir=root / "memory",
        global_memory_dir=root / "global-memory",
        tool_output_spill_dir=root / "spill",
        max_steps_per_turn=_MAX_STEPS_PER_TURN,
        auto_compact_window=window,
    )


def run_trial(
    cfg: EvalConfig,
    goal: str,
    setup: Callable[[Path], None],
    *,
    small_window: bool = False,
    timeout_s: float = TRIAL_TIMEOUT_S,
) -> tuple[Trace, dict[str, Any], Path]:
    """Drive ``goal`` once; return the trace, usage, and the scratch root.

    The caller removes the root after scoring (scoring reads the workspace).
    """
    root = Path(tempfile.mkdtemp(prefix="noeta-eval-"))
    ws = root / "ws"
    ws.mkdir()
    setup(ws)
    options = dataclasses.replace(main_options(), permission_mode="bypassPermissions")
    if cfg.system_prompt is not None:
        options = dataclasses.replace(options, system_prompt=cfg.system_prompt)
    client = Client(
        options,
        provider=PROVIDERS[cfg.provider](),
        workspace_dir=ws,
        model=cfg.model,
        multi_turn=False,
        host_config=_host_config(cfg, root, small_window),
    )
    error: Optional[str] = None
    status = "unknown"
    task_id = ""
    try:
        seeded = client.seed_start(goal=goal)
        task_id = seeded.task_id
        box: dict[str, Any] = {}

        def drive() -> None:
            try:
                box["outcome"] = client.drive_seeded(seeded)
            except Exception as exc:  # noqa: BLE001 - recorded, not raised
                box["error"] = f"{type(exc).__name__}: {exc}"

        t = threading.Thread(target=drive, daemon=True)
        t.start()
        t.join(timeout_s)
        if t.is_alive():
            error = f"timeout after {timeout_s:.0f}s"
            client.interrupt(task_id, reason="eval timeout")
            t.join(30)
            if t.is_alive():
                client.interrupt(task_id, reason="eval timeout", force=True)
                t.join(30)
        if "error" in box:
            error = box["error"]
        if "outcome" in box:
            status = str(box["outcome"].status)
        items = list(client.messages(task_id))
        events = client.events(task_id)
        spawned = sum(1 for e in events if e.type == "SubtaskSpawned")
        compactions = sum(1 for e in events if e.type == "Compacted")
        if status == "unknown":
            status = "timeout" if error and error.startswith("timeout") else "error"
        failed = [e for e in events if e.type == "TaskFailed"]
        if failed and error is None:
            p = failed[-1].payload
            error = f"TaskFailed: {getattr(p, 'reason', '')} {getattr(p, 'detail', '') or ''}"
        texts = [i.text for i in items if isinstance(i, AssistantMessage) and i.text.strip()]
        trace = Trace(
            workspace=ws,
            status=status,
            items=items,
            batches=_batches(items),
            results={i.call_id: i for i in items if isinstance(i, ToolResultView)},
            final_text=texts[-1] if texts else "",
            spawned=spawned,
            compactions=compactions,
            error=error,
        )
        u = client.usage(task_id)
        usage = {
            "requests": u.requests,
            "input_tokens": u.input_tokens,
            "cache_read_tokens": u.cache_read_tokens,
            "cache_write_tokens": u.cache_write_tokens,
            "output_tokens": u.output_tokens,
            "cost_usd": u.cost_usd,
        }
    finally:
        client.shutdown()
    return trace, usage, root


