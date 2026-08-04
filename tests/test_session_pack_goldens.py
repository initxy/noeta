"""Byte-order goldens for the session-pack construction.

These tests pin the two load-bearing orders the builder must preserve:

* the tool dict insertion order (feeds the Engine's deterministic
  ``ToolSchemaRecorded`` emission and the composed ``provider_tool_schemas``
  half of the stable-prefix hash), and
* the content-kind registration order (IS the semi_stable layout).

A change that reorders either list is a byte-equality regression, not a
refactor — the stable-prefix prompt cache only hits when the prefix is
byte-stable.

The expected lists are LITERAL on purpose — deriving them from the code under
test would make the golden follow the regression it exists to catch.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from noeta.storage.memory import InMemoryContentStore
from noeta.tools.fake import FakeTool

from tests._session_inputs import build_code_replay_inputs
from noeta.presets import official_specs


#: Default `main` build — fs pack (whitelist-filtered) + web + the memory
#: pack (main opens the memory capability); no skill indexed in a bare tmp
#: workspace, so no script tool.
GOLDEN_DEFAULT_TOOLS: list[str] = [
    "Read",
    "Glob",
    "Grep",
    "Edit",
    "Write",
    "Bash",
    "BashOutput",
    "KillShell",
    "WebFetch",
    "memory_write",
    "memory_read",
    "memory_search",
    "memory_archive",
]

#: Fully-loaded build — every optional pack present: memory, skills (script
#: tool), browser (fake backend), mcp override, custom, app (fake gateway).
GOLDEN_LOADED_TOOLS: list[str] = [
    *GOLDEN_DEFAULT_TOOLS,
    "run_skill_script",
    "browser_navigate",
    "browser_click",
    "browser_type",
    "browser_extract",
    "browser_screenshot",
    "mcp__srv__echo",
    "my_custom",
    "open_app",
]

GOLDEN_DEFAULT_KINDS: list[str] = ["skill", "memory", "environment"]
GOLDEN_LOADED_KINDS: list[str] = ["skill", "memory", "instructions", "environment"]


class _FakeBrowserBackend:
    """Structural BrowserBackend stand-in — never called at build time."""

    def navigate(self, url: str) -> str:
        return ""

    def click(self, index: int) -> str:
        return ""

    def type(self, index: int, text: str, *, submit: bool = False) -> str:
        return ""

    def extract(self) -> str:
        return ""

    def screenshot(self) -> bytes:
        return b""


class _FakeGateway:
    """Structural AppPreviewGateway stand-in — never called at build time."""

    def mount(self, **kwargs: Any) -> Any:
        raise AssertionError("build-time golden never mounts")


def _main_spec() -> Any:
    return official_specs()["main"]


def _build(tmp_path: Path, **kwargs: Any) -> Any:
    return build_code_replay_inputs(
        workspace_dir=tmp_path,
        agent=_main_spec(),
        content_store=InMemoryContentStore(),
        model="claude-sonnet-4-5",
        **kwargs,
    )


def _kinds(inputs: Any) -> list[str]:
    return list(inputs.composer._content_renderers.kinds())


def test_default_build_tool_and_kind_order(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv("NOETA_WEB_SEARCH_API_KEY", raising=False)
    inputs = _build(tmp_path)
    assert list(inputs.tools) == GOLDEN_DEFAULT_TOOLS
    assert _kinds(inputs) == GOLDEN_DEFAULT_KINDS


def test_fully_loaded_build_tool_and_kind_order(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.delenv("NOETA_WEB_SEARCH_API_KEY", raising=False)
    # A workspace-local skill so the skills stage indexes something and the
    # script tool has a reason to exist; NOETA.md so instructions load.
    skill_dir = tmp_path / ".noeta" / "skills" / "golden-skill"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\nname: golden-skill\ndescription: golden\n---\nbody\n",
        encoding="utf-8",
    )
    (skill_dir / "run.sh").write_text("#!/bin/sh\necho ok\n", encoding="utf-8")
    (tmp_path / "NOETA.md").write_text("# golden instructions\n", encoding="utf-8")

    mcp_tool = FakeTool(name="mcp__srv__echo")
    custom_tool = FakeTool(name="my_custom")

    inputs = _build(
        tmp_path,
        memory_enabled=True,
        instructions_enabled=True,
        allow_skill_scripts=True,
        capability_flags={"browser": True},
        backends={
            "browser": _FakeBrowserBackend(),
            "app_preview": _FakeGateway(),
        },
        mcp_tools_override={mcp_tool.name: mcp_tool},
        custom_tools={custom_tool.name: custom_tool},
    )
    assert list(inputs.tools) == GOLDEN_LOADED_TOOLS
    assert _kinds(inputs) == GOLDEN_LOADED_KINDS
