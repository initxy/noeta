"""The ``Memory`` extension: the four tools, the index recorded once per
task, the index-change line, auto-recall on each user message, the recall
judge, and the consolidation curator."""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import logging
from collections.abc import Awaitable, Callable, Collection, Sequence
from pathlib import Path
from typing import Any

from noeta.core.agent import Agent
from noeta.core.events import Recorded
from noeta.core.extension import ContextPart, Extension, TaskView
from noeta.core.helpers import new_user_messages, resident_budget
from noeta.core.tool import Tool
from noeta.core.types import Message, ModelRequest, SystemPart, user
from noeta.plugins.memory.index import (
    INDEX_DELTA_PREFIX,
    RECALLED_BODY_FRAME,
    format_index_delta,
    format_recall_text,
    format_recalled_body,
    index_delta_items,
    render_memory_index_text,
)
from noeta.plugins.memory.matching import MemoryEntries
from noeta.plugins.memory.recall import (
    parse_judge_reply,
    read_memory_names,
    recall_memories,
    render_judge_system,
)
from noeta.plugins.memory.store import DEFAULT_GLOBAL_MEMORY_DIR, MemoryStore
from noeta.plugins.memory.tools import (
    MEMORY_READ,
    MemoryArchiveTool,
    MemoryReadTool,
    MemorySearchTool,
    MemoryWriteTool,
    load_text,
)

__all__ = [
    "CONSOLIDATION_AGENT_NAME",
    "MEMORY_POLICY_PROMPT",
    "MEMORY_POLICY_READ_ONLY_PROMPT",
    "Memory",
    "consolidation_agent",
]

_log = logging.getLogger(__name__)

#: The memory-policy fragment a memory-enabled agent's prompt ends with.
MEMORY_POLICY_PROMPT = load_text("memory-policy", strip=False)
#: The variant for a read-only store (only ``memory_read`` / ``memory_search``).
MEMORY_POLICY_READ_ONLY_PROMPT = load_text("memory-policy-read-only", strip=False)
CONSOLIDATION_PROMPT = load_text("consolidation", strip=False)
CONSOLIDATION_AGENT_NAME = "__consolidation__"
_JUDGE_INSTRUCTIONS = load_text("recall_judge")
_JUDGE_MAX_TOKENS = 200


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class Memory(Extension):
    """Cross-session memory in a directory of markdown files.

    Constructor arguments (0.6 ``HostConfig`` / memory plugin config):

    * ``root`` — the store directory, or ``root(view)`` returning it (sync or
      async; ``None`` = the default), resolved once when the task starts
      and recorded, so the task keeps it (0.6 ``memory_dir`` /
      ``global_memory_dir`` / ``memory_root_resolver``). Default
      ``~/.noeta/memories``.
    * ``read_only`` — offer only ``memory_read`` / ``memory_search``
      (``memory_read_only``) and the read-only policy.
    * ``max_bytes`` — page cap for ``memory_write`` (``memory_max_bytes``).
    * ``index_budget_tokens`` — index budget; ``None`` = 1 % of the task
      model's window, at most 4,096.
    * ``recall`` — auto-recall and the index-change line on each user
      message (off for the consolidation curator).
    * ``recall_model`` — the judge model asked when a message named no
      memory (``Options.recall_model``); ``recall_timeout`` bounds it.
    * ``exclude`` — memory names recall never surfaces (``recall_exclude``).
    * ``rehang`` — put the index and recalled bodies right after a
      compaction's summary (``resident``).
    """

    name = "memory"

    def __init__(
        self,
        *,
        root: Any = None,
        read_only: bool = False,
        max_bytes: int | None = None,
        index_budget_tokens: int | None = None,
        recall: bool = True,
        recall_model: str | None = None,
        recall_timeout: float = 10.0,
        exclude: Collection[str] = (),
        rehang: bool = True,
    ) -> None:
        if isinstance(exclude, (str, bytes)):
            raise ValueError("Memory: exclude must be a collection of memory names")
        for label, value in (("max_bytes", max_bytes), ("index_budget_tokens",
                                                        index_budget_tokens)):
            if value is not None and (isinstance(value, bool) or value <= 0):
                raise ValueError(f"Memory: {label} must be a positive int")
        self.root = root
        self.read_only = read_only
        self.max_bytes = max_bytes
        self.index_budget_tokens = index_budget_tokens
        self.recall = recall
        self.recall_model = recall_model
        self.recall_timeout = recall_timeout
        self.exclude = frozenset(exclude)
        self.rehang = rehang
        self._tools: list[Tool] = [MemoryReadTool(self.name), MemorySearchTool(self.name)]
        if not read_only:
            self._tools = [MemoryWriteTool(self.name, max_bytes=max_bytes), *self._tools,
                           MemoryArchiveTool(self.name)]

    @property
    def policy(self) -> str:
        return MEMORY_POLICY_READ_ONLY_PROMPT if self.read_only else MEMORY_POLICY_PROMPT

    def context(self, view: TaskView) -> Sequence[ContextPart]:
        """The memory policy, after the agent's own prompt."""
        name = "memory/policy-read-only" if self.read_only else "memory/policy"
        return (ContextPart(text=view.prompt(name, self.policy)),)

    # ------------------------------------------------------------ state

    def initial_state(self) -> Any:
        return {"root": None, "index": None, "bodies": {}}

    def reduce(self, state: Any, kind: str, data: Any) -> Any:
        state = state or self.initial_state()
        if kind == "root":
            return {**state, "root": data["root"]}
        if kind == "index":
            return {**state, "index": data["text"]}
        if kind == "recalled":
            return {**state, "bodies": {**state["bodies"], data["name"]: data["hash"]}}
        return state

    def tools(self, view: TaskView) -> Sequence[Tool]:
        return self._tools

    def store(self, view: TaskView) -> MemoryStore | None:
        root = (view.ext_state(self.name) or {}).get("root")
        return MemoryStore(Path(root)) if root else None

    # ------------------------------------------------------------ hooks

    async def on_start(self, view: TaskView) -> None:
        state = view.ext_state(self.name) or {}
        if not state.get("root"):
            root = await self._resolve_root(view)
            view.record(self.name, "root", {"root": root})
        else:
            root = state["root"]
        if state.get("index") is None:
            store = MemoryStore(Path(root))
            entries, updated = await asyncio.to_thread(store.index_snapshot)
            if entries:
                text = render_memory_index_text(entries, budget_tokens=self._budget(view),
                                                updated=updated)
                view.record(self.name, "index", {"text": text},
                            message=user(text, origin="system"))

    async def on_event(self, view: TaskView, recorded: Recorded) -> None:
        if not self.recall:
            return
        messages = new_user_messages(view.state, recorded.seq)
        store = self.store(view)
        if not messages or store is None:
            return
        for message in messages:
            await self._recall(view, store, message.text)
        await self._delta(view, store)

    # ------------------------------------------------------------ recall

    async def _recall(self, view: TaskView, store: MemoryStore, text: str) -> None:
        state = view.ext_state(self.name) or {}
        pinned: dict[str, str] = state.get("bodies") or {}
        visible = {n for n in pinned if self._body_visible(view, n)}
        changed = set()
        for name, digest in pinned.items():
            current = await asyncio.to_thread(store.read, name)
            if current is not None and _sha(current) != digest:
                changed.add(name)
        resident = (visible | read_memory_names(view.state.messages, MEMORY_READ)) - changed
        judge = self._judge(view) if self.recall_model else None
        hits = await recall_memories(store, text, resident=resident, exclude=self.exclude,
                                     judge=judge)
        for hit in hits:
            if hit.full:
                view.record(self.name, "recalled", {"name": hit.name, "hash": _sha(hit.text)},
                            message=user(format_recalled_body(hit.name, hit.text),
                                         origin="memory"))
        pointers = [h for h in hits if not h.full]
        if pointers:
            view.record(self.name, "pointers", {"names": [h.name for h in pointers]},
                        message=user(format_recall_text(pointers), origin="memory"))

    @staticmethod
    def _body_visible(view: TaskView, name: str) -> bool:
        head = f"{RECALLED_BODY_FRAME}\n\n## {name}\n"
        return any(m.origin == "memory" and m.text.startswith(head) for m in view.state.messages)

    def _judge(self, view: TaskView) -> Callable[[MemoryEntries, str],
                                                 Awaitable[tuple[str, ...]]] | None:
        complete = view.complete
        model = self.recall_model
        if complete is None or not model:
            return None
        budget = self._budget(view)

        async def judge(entries: MemoryEntries, text: str) -> tuple[str, ...]:
            if not entries:
                return ()
            system = render_judge_system(
                entries, view.prompt("memory/recall_judge", _JUDGE_INSTRUCTIONS),
                budget_tokens=budget)
            request = ModelRequest(model=model, system=[SystemPart(text=system, cache=True)],
                                   messages=[user(text)], max_tokens=_JUDGE_MAX_TOKENS,
                                   thinking=False)
            try:
                response = await asyncio.wait_for(complete(request), self.recall_timeout)
            except Exception:  # noqa: BLE001 — a judge failure is a lexical miss
                _log.info("memory: recall judge failed; treating as a miss", exc_info=True)
                return ()
            return parse_judge_reply(response.message.text, entries)

        return judge

    async def _delta(self, view: TaskView, store: MemoryStore) -> None:
        snapshot = (view.ext_state(self.name) or {}).get("index")
        if snapshot is None:
            return
        entries, updated = await asyncio.to_thread(store.index_snapshot)
        text = format_index_delta(index_delta_items(snapshot, entries, updated))
        if text is None:
            return
        last = next((m.text for m in reversed(view.state.messages)
                     if m.origin == "system" and m.text.startswith(INDEX_DELTA_PREFIX)), None)
        if text != last:
            view.record(self.name, "delta", {}, message=user(text, origin="system"))

    # ------------------------------------------------------------ helpers

    async def _resolve_root(self, view: TaskView) -> str:
        root = self.root
        if callable(root):
            root = root(view)
            if inspect.isawaitable(root):
                root = await root
        return str(Path(root).expanduser() if root is not None else DEFAULT_GLOBAL_MEMORY_DIR)

    def _budget(self, view: TaskView) -> int:
        if self.index_budget_tokens is not None:
            return self.index_budget_tokens
        return resident_budget(str(view.state.config.get("model") or ""))

    async def resident(self, view: TaskView) -> Sequence[Message]:
        """After a compaction: the index, then every recalled body (read
        again; its new hash is recorded so recall stays silent on it)."""
        if not self.rehang:
            return ()
        state = view.ext_state(self.name) or {}
        out: list[Message] = []
        if state.get("index") is not None:
            out.append(user(state["index"], origin="system"))
        store = self.store(view)
        if store is None:
            return out
        for name in state.get("bodies") or {}:
            text = await asyncio.to_thread(store.read, name)
            if text is not None:
                view.record(self.name, "recalled", {"name": name, "hash": _sha(text)})
                out.append(user(format_recalled_body(name, text), origin="memory"))
        return out


def consolidation_agent(model: str, *, root: Any = None, **memory: Any) -> Agent:
    """The background curator (ADR-035): an ordinary one-shot agent with only
    the memory tools and the index (no recall), driven by the host with a
    digest of recent sessions as its message. Register it under
    :data:`CONSOLIDATION_AGENT_NAME` next to ``main``."""
    return Agent(
        name=CONSOLIDATION_AGENT_NAME,
        model=model,
        system_prompt=CONSOLIDATION_PROMPT,
        extensions=(Memory(root=root, recall=False, read_only=False, **memory),),
        one_shot=True,
    )
