"""The bytes the default agents send, locked: one scripted session through
``default_agents`` with every built-in context source (project instructions,
skills, memory, environment, todo reminder, a sub-agent), rendered by each
provider adapter and compared with ``golden/requests.json`` (a hash per
request, and the full body of each agent's last request).

A change that alters what the model sees fails here. When the change is
intended, rerun with ``NOETA_UPDATE_GOLDEN=1`` and review the diff.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path
from typing import Any

import msgspec
import pytest

from noeta.agents import default_agents
from noeta.backends.execenvs.local import LocalExecEnv
from noeta.backends.providers.anthropic import AnthropicProvider
from noeta.backends.providers.openai_compat import OpenAICompatProvider
from noeta.backends.providers.openai_responses import OpenAIResponsesProvider
from noeta.backends.stores.sqlite import SqliteStore
from noeta.core.client import Client
from noeta.core.runner import Runtime
from noeta.core.types import Message, ModelRequest, ModelResponse, Text, ToolUse, Usage
from noeta.plugins.memory import memory_dir_for
from noeta.toolkit.context import project_slug

GOLDEN = Path(__file__).parent / "golden" / "requests.json"
MODEL = "claude-sonnet-4-6"


def _reply(text: str = "", *calls: tuple[str, str, dict[str, Any]]) -> ModelResponse:
    content: list[Any] = [Text(text=text)] if text else []
    content += [ToolUse(id=i, name=n, input=a) for i, n, a in calls]
    return ModelResponse(message=Message(role="assistant", content=content),
                         stop_reason="tool_use" if calls else "end_turn",
                         usage=Usage(uncached=10, output=5))


class Router:
    """The main agent's script and the Explore sub-agent's script, told
    apart by the system prompt; keeps every request in order."""

    def __init__(self, main: list[ModelResponse], child: list[ModelResponse]) -> None:
        self.main, self.child = main, child
        self.requests: list[tuple[str, ModelRequest]] = []

    async def complete(self, request: ModelRequest, on_delta: Any = None) -> ModelResponse:
        child = any(b.text.startswith("You are a file-search specialist") for b in request.system)
        who = "child" if child else "main"
        self.requests.append((who, request))
        script = self.child if who == "child" else self.main
        assert script, f"{who} script ran out"
        return script.pop(0)


async def _no_sleep(_: float) -> None:
    return None


def _normalize(text: str, tmp: str) -> str:
    text = text.replace(tmp, "<TMP>").replace(project_slug(tmp), "<TMP-SLUG>")
    text = re.sub(r"\d{4}-\d{2}-\d{2}", "<DATE>", text)
    text = re.sub(r"(?m)^ - (Platform|Shell|OS Version): .*$", r" - \1: <X>", text)
    text = re.sub(r"<duration_ms>\d+</duration_ms>", "<duration_ms>N</duration_ms>", text)
    return re.sub(r"\b[0-9a-f]{32}\b", "<ID>", text)


async def test_default_agent_requests_match_golden(tmp_path: Path,
                                                   monkeypatch: pytest.MonkeyPatch) -> None:
    home = tmp_path / "home"
    work = tmp_path / "work"
    (work / ".agents" / "skills" / "release-notes").mkdir(parents=True)
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    (work / "AGENTS.md").write_text("Run `make check` before committing.\n")
    (work / ".agents" / "skills" / "release-notes" / "SKILL.md").write_text(
        "---\nname: release-notes\ndescription: Draft release notes from git log.\n---\n"
        "Read git log and summarize.\n")
    (work / "notes.txt").write_text("the secret word is tangerine\n")
    noeta_home = tmp_path / "noeta-home"
    mem = Path(memory_dir_for(str(work), noeta_home))
    mem.mkdir(parents=True)
    (mem / "deploy-steps.md").write_text(
        "---\nname: deploy-steps\ndescription: How we deploy.\nmetadata:\n  type: project\n"
        "---\nTag, then wait for the release run.\n")
    (mem / "MEMORY.md").write_text("- [Deploy steps](deploy-steps.md) — tag, then wait\n")

    todos = [{"content": "Read notes", "status": "in_progress", "activeForm": "Reading notes"}]
    main = [
        _reply("Planning.", ("t1", "TodoWrite", {"todos": todos})),
        _reply("", ("r1", "Read", {"file_path": str(work / "notes.txt")}),
               ("g1", "Glob", {"pattern": "*.txt"})),
        _reply("", ("k1", "Agent", {"description": "find notes", "prompt": "Find notes.txt.",
                                    "subagent_type": "Explore"})),
        _reply("Started an agent to look."),
        *[_reply("", (f"b{i}", "Bash", {"command": f"echo {i}", "description": "echo"}))
          for i in range(11)],
        _reply("The word is tangerine."),
        _reply("Deploy: tag, then wait."),
    ]
    child = [
        _reply("", ("c1", "Read", {"file_path": str(work / "notes.txt")})),
        _reply("Found it.", ("c2", "SubagentHandback",
                             {"message": "notes.txt is in the root."})),
    ]
    router = Router(main, child)
    store = SqliteStore(str(tmp_path / "t.db"))
    agents = default_agents(MODEL, plugins={"memory": {"home": str(noeta_home)}})
    rt = Runtime(store=store, agents={a.name: a for a in agents},
                 provider_for=lambda _m: router,  # type: ignore[arg-type,return-value]
                 env_for=lambda s: LocalExecEnv(str(s.config.get("cwd") or work)),
                 sleep=_no_sleep)
    client = Client(store)
    tid = await client.create("main", "What is the secret word in notes.txt?",
                              task_id="t" * 32,
                              config={"cwd": str(work), "permission_mode": "bypassPermissions"})
    await rt.run_until_idle()
    await client.send(tid, "And how do we deploy?")
    await rt.run_until_idle()
    await store.close()
    assert not main and not child

    adapters = {
        "anthropic": AnthropicProvider(api_key="x", device_id="d" * 64),
        "openai_compat": OpenAICompatProvider(api_key="x", base_url="http://x"),
        "openai_responses": OpenAIResponsesProvider(api_key="x", base_url="http://x"),
    }
    def dump(obj: Any) -> str:
        return _normalize(json.dumps(msgspec.to_builtins(obj), indent=1, ensure_ascii=False,
                                     sort_keys=True), str(tmp_path))

    # Every request by hash; each agent's last request in full (it carries
    # the whole history, so most changes show up there as a readable diff).
    bodies = [(who, {n: a.build_body(r) for n, a in adapters.items()})
              for who, r in router.requests]
    last = {who: body for who, body in bodies}
    text = dump({
        "hashes": [f"{who} " + hashlib.sha256(dump(body).encode()).hexdigest()[:16]
                   for who, body in bodies],
        "last": last,
    })
    if os.environ.get("NOETA_UPDATE_GOLDEN") == "1":
        GOLDEN.parent.mkdir(exist_ok=True)
        GOLDEN.write_text(text + "\n")
    assert text + "\n" == GOLDEN.read_text(), "requests changed; see module docstring"
