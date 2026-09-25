"""SDK audit closure WP-E — built-in tools, ExecEnv, shell policy.

One regression per reproduced defect (E1-E12 of
``docs/implementation-specs/sdk-audit-closure-2026-09-25.md``). Nothing here
touches the network: HTTP runs through ``httpx.MockTransport``, the sandbox
backend through a scripted transport, and the only real processes are local
``bash`` / ``git`` in ``tmp_path``.
"""

from __future__ import annotations

import base64
import hashlib
import shutil
import subprocess
import warnings
from pathlib import Path
from typing import Any, Mapping, Optional

import httpx
import pytest

from noeta.builtins.fs import impl as fs_impl
from noeta.builtins.fs.impl import (
    GlobTool,
    GrepTool,
    ReadFileTool,
    ReplaceTextTool,
    ShellRunTool,
    WriteFileTool,
    build_fs_tools,
)
from noeta.builtins.fs.impl.shell_rules import DEFAULT_SHELL_RULES
from noeta.builtins.web.impl import (
    ContainerCurlFetchTransport,
    HttpFetchTransport,
    LLMPageDigester,
    WebFetchTool,
    build_web_tools,
)
from noeta.client.host import _make_shell_approval_predicate
from noeta.client.plugins import grant_trust
from noeta.protocols.messages import LLMRequest, LLMResponse, TextBlock, Usage
from noeta.protocols.step_context import StepContext
from noeta.protocols.tool import ToolContext
from noeta.runtime import subproc
from noeta.runtime.exec_env import LocalExecEnv
from noeta.runtime.shell_policy import (
    ShellMode,
    build_allowlist,
    command_in_allowlist,
)
from noeta.runtime.subproc import RunOutcome, run_argv
from noeta.runtime.tool import InMemoryFileReadRegistry
from noeta.runtime.workspace import FsWriteMode, WorkspaceRoot
from noeta.storage.memory import InMemoryContentStore
from tests.test_aio_sandbox_exec_env import FakeAio, _env, _exec_ok, _ok
from tests.test_project_shell_allowlist_trust import (
    _build,
    _host,
    _predicate,
    _workspace,
)


def _ctx() -> ToolContext:
    return ToolContext(
        artifact_store=InMemoryContentStore(),
        file_read_registry=InMemoryFileReadRegistry(),
    )


# ---------------------------------------------------------------------------
# E1 — unquoted expansion characters are never allowlisted
# ---------------------------------------------------------------------------

_RULES = build_allowlist((), base_rules=DEFAULT_SHELL_RULES)


@pytest.mark.parametrize(
    "command",
    [
        "find . -maxdepth 0 {-exec,touch} /tmp/x {} +",
        "rg --files {--pre,touch} x",
        "git diff {--output,/dev/null}",
        "grep -rn foo *.py",
        "ls a?",
        "ls a[bc]",
        "ls ~",
        "ls ~/other",
        "ls --dir=~/x",
    ],
)
def test_unquoted_expansion_is_never_allowlisted(command: str) -> None:
    assert command_in_allowlist(command, _RULES) is False
    needs = _make_shell_approval_predicate(_RULES)
    assert needs("Bash", {"command": command}) is True


@pytest.mark.parametrize(
    "command",
    [
        "find . -name '*.py'",
        'find . -name "*.py"',
        "find . -name \\*.py",
        "rg 'a{1,2}' src",
        "grep -rn \"x[0-9]\" .",
        "git log HEAD~1",
        "git log -n 5 --oneline",
    ],
)
def test_quoted_or_escaped_expansion_stays_allowlisted(command: str) -> None:
    assert command_in_allowlist(command, _RULES) is True


# ---------------------------------------------------------------------------
# E2 — test runners are trust-gated, git runs hardened
# ---------------------------------------------------------------------------


def test_test_runner_rules_need_a_trusted_workspace(tmp_path: Path) -> None:
    ws = _workspace(tmp_path, None)
    untrusted = _predicate(_build(_host(ws, tmp_path)))
    for command in ("pytest -q", "uv run pytest -x", "npm test", "pnpm test"):
        assert untrusted("Bash", {"command": command}) is True, command
    # Read-only rules are unaffected by trust.
    assert untrusted("Bash", {"command": "git status"}) is False
    assert untrusted("Bash", {"command": "ls -la"}) is False

    grant_trust(ws, tmp_path / "trust.json")
    trusted = _predicate(_build(_host(ws, tmp_path)))
    for command in ("pytest -q", "uv run pytest -x", "npm test", "pnpm test"):
        assert trusted("Bash", {"command": command}) is False, command

    opened = _predicate(
        _build(_host(ws, tmp_path, project_shell_allowlist_trust="open"))
    )
    assert opened("Bash", {"command": "pytest -q"}) is False


class _RecordingRunner:
    def __init__(self) -> None:
        self.argvs: list[list[str]] = []

    def __call__(self, argv: list[str], **_: Any) -> subprocess.CompletedProcess[bytes]:
        self.argvs.append(list(argv))
        return subprocess.CompletedProcess(argv, 0, b"", b"")


@pytest.mark.parametrize("mode", [ShellMode.ARBITRARY, ShellMode.ALLOWLIST])
def test_allowlisted_git_runs_with_hardening_flags(
    tmp_path: Path, mode: ShellMode
) -> None:
    runner = _RecordingRunner()
    tool = ShellRunTool(
        workspace=WorkspaceRoot.from_path(tmp_path), mode=mode, runner=runner
    )
    for command in ("git status --short", "git diff --stat -- a", "git log -p"):
        assert tool.invoke({"command": command}, _ctx()).success
    assert runner.argvs == [
        ["git", "-c", "core.fsmonitor=", "status", "--short"],
        [
            "git", "-c", "core.fsmonitor=", "diff", "--no-ext-diff",
            "--no-textconv", "--stat", "--", "a",
        ],
        ["git", "log", "--no-ext-diff", "--no-textconv", "-p"],
    ]


def test_non_git_and_unmatched_commands_still_run_through_bash(
    tmp_path: Path,
) -> None:
    runner = _RecordingRunner()
    tool = ShellRunTool(
        workspace=WorkspaceRoot.from_path(tmp_path),
        mode=ShellMode.ARBITRARY,
        runner=runner,
    )
    tool.invoke({"command": "ls -la"}, _ctx())
    tool.invoke({"command": "git status | cat"}, _ctx())
    assert runner.argvs == [
        ["bash", "-c", "ls -la"],
        ["bash", "-c", "git status | cat"],
    ]


@pytest.mark.skipif(shutil.which("git") is None, reason="needs git")
def test_repo_configured_git_helpers_do_not_run(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    marker_fsmonitor = tmp_path / "fsmonitor-ran"
    marker_ext = tmp_path / "extdiff-ran"
    hook = tmp_path / "hook.sh"
    hook.write_text(f"#!/bin/sh\ntouch {marker_fsmonitor}\n")
    hook.chmod(0o755)
    ext = tmp_path / "ext.sh"
    ext.write_text(f"#!/bin/sh\ntouch {marker_ext}\n")
    ext.chmod(0o755)
    env = {"GIT_CONFIG_NOSYSTEM": "1", "HOME": str(tmp_path), "PATH": "/usr/bin:/bin"}

    def git(*args: str) -> None:
        subprocess.run(["git", *args], cwd=repo, env=env, check=True,
                       capture_output=True)

    git("init", "-q")
    (repo / "f.txt").write_text("a\n")
    git("add", "f.txt")
    git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "x")
    (repo / "f.txt").write_text("b\n")
    git("config", "core.fsmonitor", str(hook))
    git("config", "diff.external", str(ext))

    tool = ShellRunTool(
        workspace=WorkspaceRoot.from_path(repo), mode=ShellMode.ARBITRARY
    )
    assert tool.invoke({"command": "git status"}, _ctx()).success
    assert tool.invoke({"command": "git diff"}, _ctx()).success
    assert not marker_fsmonitor.exists()
    assert not marker_ext.exists()
    # Sanity: the same repo DOES run them through plain bash.
    subprocess.run(["bash", "-c", "git diff"], cwd=repo, capture_output=True)
    assert marker_ext.exists()


# ---------------------------------------------------------------------------
# E3 — output is capped while streaming
# ---------------------------------------------------------------------------


def test_default_runner_keeps_only_a_bounded_tail(tmp_path: Path) -> None:
    proc = subproc._default_run(
        ["bash", "-c", "head -c 5000000 /dev/zero; printf END"],
        cwd=str(tmp_path),
        env={"PATH": "/usr/bin:/bin"},
        timeout=30,
        output_cap=100,
    )
    assert len(proc.stdout) == 101  # cap + 1: enough to flag the overflow
    assert proc.stdout.endswith(b"END")


def test_run_argv_renders_like_cap_stream(tmp_path: Path) -> None:
    outcome = run_argv(
        ["bash", "-c", "seq 1 100000; echo err >&2"],
        cwd=tmp_path,
        timeout_s=30,
        output_cap=64,
    )
    full = subprocess.run(["seq", "1", "100000"], capture_output=True).stdout
    assert outcome.stdout == full[-64:]
    assert outcome.stdout_truncated is True
    assert outcome.stderr == b"err\n"
    assert outcome.stderr_truncated is False


def test_timeout_keeps_partial_output_and_kills(tmp_path: Path) -> None:
    outcome = run_argv(
        ["bash", "-c", "echo hi; sleep 30"],
        cwd=tmp_path,
        timeout_s=1,
        output_cap=1024,
    )
    assert outcome.timed_out is True
    assert outcome.returncode == -1
    assert outcome.stdout == b"hi\n"


def test_injected_runner_keeps_the_subprocess_run_shape(tmp_path: Path) -> None:
    seen: dict[str, Any] = {}

    def runner(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        seen.update(kwargs)
        return subprocess.CompletedProcess(argv, 0, b"x" * 50, b"")

    outcome = run_argv(["x"], cwd=tmp_path, timeout_s=5, output_cap=10, runner=runner)
    assert "output_cap" not in seen
    assert outcome.stdout == b"x" * 10 and outcome.stdout_truncated


# ---------------------------------------------------------------------------
# E4 — Read streams the window, caps what it serves
# ---------------------------------------------------------------------------


class _NoWholeReads(LocalExecEnv):
    def read_bytes(self, path: Path) -> bytes:  # pragma: no cover - must not run
        raise AssertionError("Read must not load the whole file")


def test_read_streams_the_window_and_hashes_the_whole_file(tmp_path: Path) -> None:
    body = b"".join(b"line %d\n" % i for i in range(1, 300_001))  # ~3.3 MB
    (tmp_path / "big.log").write_bytes(body)
    ctx = _ctx()
    tool = ReadFileTool(
        workspace=WorkspaceRoot.from_path(tmp_path), exec_env=_NoWholeReads()
    )
    result = tool.invoke(
        {"file_path": "big.log", "offset": 250_000, "limit": 3}, ctx
    )
    assert result.success
    assert result.output.startswith(
        "250000\tline 250000\n250001\tline 250001\n250002\tline 250002"
    )
    assert "of 300000 total lines" in result.output
    # Over 1 MiB: the artifact is the served window, not 3 MB.
    assert result.artifacts[0].size < 100
    assert ctx.file_read_registry is not None
    assert ctx.file_read_registry.digest(
        str((tmp_path / "big.log").resolve())
    ) == hashlib.sha256(body).hexdigest()


def test_read_caps_served_text_with_an_actionable_note(tmp_path: Path) -> None:
    (tmp_path / "wide.txt").write_text(("y" * 150 + "\n") * 3000)
    result = ReadFileTool(workspace=WorkspaceRoot.from_path(tmp_path)).invoke(
        {"file_path": "wide.txt"}, _ctx()
    )
    assert result.success
    body, _, notes = result.output.partition("\n\n(")
    assert len(body.encode("utf-8")) <= 100 * 1024
    assert "Output capped at 100 KB" in notes
    shown = len(body.splitlines())
    assert f"Use offset={shown + 1}" in notes


def test_read_clips_a_huge_single_line_without_buffering_it(tmp_path: Path) -> None:
    (tmp_path / "min.js").write_bytes(b"x" * (8 * 1024 * 1024) + b"\nnext\n")
    result = ReadFileTool(
        workspace=WorkspaceRoot.from_path(tmp_path), exec_env=_NoWholeReads()
    ).invoke({"file_path": "min.js"}, _ctx())
    lines = result.output.splitlines()
    assert lines[0].endswith("… [line truncated]")
    assert lines[1] == "     2\tnext"


def test_read_small_file_still_stores_the_whole_body(tmp_path: Path) -> None:
    (tmp_path / "a.txt").write_text("one\ntwo\nthree\n")
    result = ReadFileTool(workspace=WorkspaceRoot.from_path(tmp_path)).invoke(
        {"file_path": "a.txt", "limit": 1}, _ctx()
    )
    assert result.artifacts[0].size == len(b"one\ntwo\nthree\n")


# ---------------------------------------------------------------------------
# E5 — CRLF files are edited with their own line ending
# ---------------------------------------------------------------------------


def _edit_env(tmp_path: Path) -> tuple[ToolContext, ReadFileTool, ReplaceTextTool]:
    ws = WorkspaceRoot.from_path(tmp_path)
    return (
        _ctx(),
        ReadFileTool(workspace=ws),
        ReplaceTextTool(workspace=ws, mode=FsWriteMode.APPLY),
    )


def test_crlf_multi_line_edit_matches_and_keeps_crlf(tmp_path: Path) -> None:
    target = tmp_path / "crlf.txt"
    target.write_bytes(b"alpha\r\nbeta\r\ngamma\r\n")
    ctx, read, edit = _edit_env(tmp_path)
    read.invoke({"file_path": "crlf.txt"}, ctx)
    result = edit.invoke(
        {"file_path": "crlf.txt", "old_string": "alpha\nbeta", "new_string": "A\nB"},
        ctx,
    )
    assert result.success, result.summary
    assert target.read_bytes() == b"A\r\nB\r\ngamma\r\n"


def test_crlf_single_line_edit_writes_crlf_breaks(tmp_path: Path) -> None:
    target = tmp_path / "crlf.txt"
    target.write_bytes(b"alpha\r\ngamma\r\n")
    ctx, read, edit = _edit_env(tmp_path)
    read.invoke({"file_path": "crlf.txt"}, ctx)
    assert edit.invoke(
        {"file_path": "crlf.txt", "old_string": "gamma", "new_string": "G\nH"}, ctx
    ).success
    assert target.read_bytes() == b"alpha\r\nG\r\nH\r\n"


def test_lf_file_edit_is_unchanged(tmp_path: Path) -> None:
    target = tmp_path / "lf.txt"
    target.write_bytes(b"a\nb\n")
    ctx, read, edit = _edit_env(tmp_path)
    read.invoke({"file_path": "lf.txt"}, ctx)
    assert edit.invoke(
        {"file_path": "lf.txt", "old_string": "a\nb", "new_string": "x\ny"}, ctx
    ).success
    assert target.read_bytes() == b"x\ny\n"


# ---------------------------------------------------------------------------
# E6 — Glob brace alternatives
# ---------------------------------------------------------------------------


def test_glob_supports_brace_alternatives(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    for name in ("a.ts", "b.tsx", "c.js", "d.md"):
        (tmp_path / "src" / name).write_text("x\n")
    tool = GlobTool(workspace=WorkspaceRoot.from_path(tmp_path))
    out = tool.invoke({"pattern": "**/*.{ts,tsx}"}, _ctx()).output
    assert sorted(out.splitlines()) == ["src/a.ts", "src/b.tsx"]
    out = tool.invoke({"pattern": "src/*.{j{s,x},md}"}, _ctx()).output
    assert sorted(out.splitlines()) == ["src/c.js", "src/d.md"]


# ---------------------------------------------------------------------------
# E7 — WebFetch honours Content-Type, origin, curl -g, digest headers
# ---------------------------------------------------------------------------


def _http_tool(handler: Any) -> WebFetchTool:
    client = httpx.Client(transport=httpx.MockTransport(handler))
    return WebFetchTool(transport=HttpFetchTransport(client=client))


_C_SOURCE = "#include <stdio.h>\nint main(void) { return a<b && c>d; }\n"


@pytest.mark.parametrize(
    "content_type,body",
    [
        ("text/x-csrc", _C_SOURCE),
        ("text/markdown; charset=utf-8", "# Title\n\n- <b>item</b> & more\n"),
        ("application/json", '{"a": "<tag>"}'),
    ],
)
def test_text_bodies_are_returned_as_is(content_type: str, body: str) -> None:
    tool = _http_tool(
        lambda request: httpx.Response(
            200, content=body.encode(), headers={"content-type": content_type}
        )
    )
    result = tool.invoke({"url": "https://example.com/f", "prompt": "x"}, _ctx())
    assert result.success
    assert body.strip() in result.output


@pytest.mark.parametrize(
    "content_type",
    ["image/png", "application/pdf", "application/octet-stream"],
)
def test_binary_bodies_fail_naming_the_type(content_type: str) -> None:
    tool = _http_tool(
        lambda request: httpx.Response(
            200, content=b"\x89PNG\r\n\x1a\n\x00\x00",
            headers={"content-type": content_type},
        )
    )
    result = tool.invoke({"url": "https://example.com/f", "prompt": "x"}, _ctx())
    assert result.success is False
    assert content_type in result.summary


@pytest.mark.parametrize(
    "headers,html",
    [
        ({"content-type": "text/html; charset=gbk"}, "<p>{}</p>"),
        (
            {"content-type": "text/html"},
            '<html><head><meta charset="gbk"></head><body><p>{}</p></body></html>',
        ),
        (
            {"content-type": "text/html"},
            '<meta http-equiv="Content-Type" content="text/html; charset=gbk">'
            "<p>{}</p>",
        ),
    ],
)
def test_charset_from_header_or_meta(headers: dict[str, str], html: str) -> None:
    raw = html.replace("{}", "中文页面").encode("gbk")
    tool = _http_tool(lambda request: httpx.Response(200, content=raw, headers=headers))
    result = tool.invoke({"url": "https://example.com/", "prompt": "x"}, _ctx())
    assert "中文页面" in result.output


@pytest.mark.parametrize(
    "location",
    ["http://example.com/plain", "https://example.com:8443/other-port"],
)
def test_redirect_changing_scheme_or_port_is_not_followed(location: str) -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return httpx.Response(302, headers={"location": location})

    result = _http_tool(handler).invoke(
        {"url": "https://example.com/start", "prompt": "x"}, _ctx()
    )
    assert calls == ["https://example.com/start"]
    assert "redirect was not followed" in result.output
    assert location in result.output


class _Container:
    """Scripted merged-stream container: body, then curl's write-out lines."""

    def __init__(self, body: bytes, content_type: Optional[str]) -> None:
        self.body = body
        self.content_type = content_type
        self.argvs: list[list[str]] = []

    def run_argv(self, argv: list[str], **_: Any) -> RunOutcome:
        self.argvs.append(list(argv))
        fmt = argv[argv.index("-w") + 1].replace("%{stderr}", "")
        out = fmt.replace("%{http_code}", "200").replace("%{redirect_url}", "")
        if self.content_type is None:
            out = out.split("\n", 1)[0] + "\n"
        else:
            out = out.replace("%{content_type}", self.content_type)
        return RunOutcome(0, 1, self.body + out.encode(), b"", False, False, False)


def test_container_fetch_globoff_and_content_type() -> None:
    container = _Container(b"\x89PNG....", "image/png")
    tool = build_web_tools(exec_env=container)["WebFetch"]  # type: ignore[arg-type]
    result = tool.invoke({"url": "https://example.com/x[1].png", "prompt": "x"}, _ctx())
    assert "-g" in container.argvs[0]
    assert result.success is False and "image/png" in result.summary

    text = _Container(_C_SOURCE.encode(), "text/plain")
    assert _C_SOURCE.strip() in ContainerCurlFetchTransport(
        exec_env=text  # type: ignore[arg-type]
    ).fetch("https://example.com/a.c")


class _HeaderAwareProvider:
    def __init__(self) -> None:
        self.headers: Optional[dict[str, str]] = None
        self.plain_calls = 0

    def complete(self, request: LLMRequest) -> LLMResponse:
        self.plain_calls += 1
        return self._answer()

    def complete_with_headers(
        self, request: LLMRequest, request_headers: Optional[dict[str, str]]
    ) -> LLMResponse:
        self.headers = request_headers
        return self._answer()

    @staticmethod
    def _answer() -> LLMResponse:
        return LLMResponse(
            stop_reason="end_turn",
            content=[TextBlock(text="digested")],
            usage=Usage(uncached=1, output=1),
            raw={},
        )


def test_digest_carries_provider_headers_for_the_calling_task() -> None:
    provider = _HeaderAwareProvider()

    def headers(step: StepContext) -> Mapping[str, str]:
        return {"x-task": step.task_id, "x-trace": step.trace_id}

    tool = build_web_tools(
        digester=LLMPageDigester(provider=provider, model="m"),  # type: ignore[arg-type]
        provider_headers=headers,
    )["WebFetch"]
    tool.transport = HttpFetchTransport(  # type: ignore[attr-defined]
        client=httpx.Client(
            transport=httpx.MockTransport(
                lambda r: httpx.Response(200, text="<p>hello</p>",
                                         headers={"content-type": "text/html"})
            )
        )
    )
    ctx = ToolContext(
        artifact_store=InMemoryContentStore(),
        metadata={"task_id": "t-1", "trace_id": "tr-1"},
    )
    result = tool.invoke({"url": "https://example.com/", "prompt": "x"}, ctx)
    assert "digested" in result.output
    assert provider.headers == {"x-task": "t-1", "x-trace": "tr-1"}
    assert provider.plain_calls == 0


# ---------------------------------------------------------------------------
# E8 — line numbers split on \n only, like rg
# ---------------------------------------------------------------------------


def test_read_numbering_matches_grep_across_odd_separators(tmp_path: Path) -> None:
    (tmp_path / "ff.py").write_text(
        "a = 1\n\x0c\nb = 2\u2028c = 3\x1cd\nTARGET = 3\n"
    )
    ws = WorkspaceRoot.from_path(tmp_path)
    grep = GrepTool(workspace=ws).invoke(
        {"pattern": "TARGET", "output_mode": "content", "-n": True}, _ctx()
    )
    assert "ff.py:4:TARGET = 3" in grep.output
    read = ReadFileTool(workspace=ws).invoke(
        {"file_path": "ff.py", "offset": 4, "limit": 1}, _ctx()
    )
    assert read.output.startswith("     4\tTARGET = 3")


def test_edit_snippet_numbers_match_read(tmp_path: Path) -> None:
    (tmp_path / "f.txt").write_text("a\x0cb\nc\nd\n")
    ctx, read, edit = _edit_env(tmp_path)
    read.invoke({"file_path": "f.txt"}, ctx)
    result = edit.invoke(
        {"file_path": "f.txt", "old_string": "d", "new_string": "D"}, ctx
    )
    assert "     3\tD" in result.output


# ---------------------------------------------------------------------------
# E9 — Write path globs: ``*`` stays in one segment
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "globs,path,allowed",
    [
        (("plans/*.md",), "plans/x.md", True),
        (("plans/*.md",), "plans/deep/x.md", False),
        (("plans/**/*.md",), "plans/deep/nested/x.md", True),
        (("plans/**/*.md",), "plans/x.md", True),
        (("*.{md,txt}",), "notes.txt", True),
        (("*.md",), "sub/x.md", False),
    ],
)
def test_write_globs_star_does_not_cross_slash(
    tmp_path: Path, globs: tuple[str, ...], path: str, allowed: bool
) -> None:
    tool = WriteFileTool(
        workspace=WorkspaceRoot.from_path(tmp_path),
        mode=FsWriteMode.APPLY,
        allowed_path_globs=globs,
    )
    result = tool.invoke({"file_path": path, "content": "x"}, _ctx())
    assert result.success is allowed, result.summary


# ---------------------------------------------------------------------------
# E10 / E11 — sandbox exit code and spill reads
# ---------------------------------------------------------------------------


def test_sandbox_missing_exit_code_is_a_failed_run() -> None:
    fake = FakeAio({"/v1/shell/exec": _ok({"output": "done"})})
    outcome = _env(fake).run_argv(["x"], cwd=Path("/w"), timeout_s=5, output_cap=99)
    assert outcome.returncode == -1
    assert b"no usable exit code" in outcome.stderr


def test_sandbox_spill_second_overflow_is_read_from_its_own_spill() -> None:
    tail = "é".encode("utf-8") * 8  # 16 bytes
    fake = FakeAio(
        {
            "/v1/shell/exec": [
                _exec_ok(output="INLINE", full_output_file_path="/tmp/one.log"),
                # The encoded tail overflowed the inline echo again.
                _exec_ok(output="trunc", full_output_file_path="/tmp/two.log"),
            ],
            "/v1/file/read": _ok({"content": base64.b64encode(tail).decode()}),
        }
    )
    outcome = _env(fake).run_argv(["big"], cwd=Path("/w"), timeout_s=5, output_cap=11)
    assert fake.calls[2][1] == {"file": "/tmp/two.log"}
    # cap 11 cuts inside a 2-byte character; the kept tail starts on a boundary.
    assert outcome.stdout_truncated is True
    assert outcome.stdout == "é".encode("utf-8") * 5
    outcome.stdout.decode("utf-8")  # strict: no partial character left


# ---------------------------------------------------------------------------
# E12 — one warning when rg is missing
# ---------------------------------------------------------------------------


def test_fs_pack_warns_once_without_ripgrep(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(fs_impl, "_RG_WARNED", False)
    monkeypatch.setattr(fs_impl.shutil, "which", lambda name: None)
    ws = WorkspaceRoot.from_path(tmp_path)
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        build_fs_tools(ws)
        build_fs_tools(ws)
    rg_warnings = [w for w in caught if "ripgrep" in str(w.message)]
    assert len(rg_warnings) == 1


def test_fs_pack_is_silent_with_ripgrep(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(fs_impl, "_RG_WARNED", False)
    monkeypatch.setattr(fs_impl.shutil, "which", lambda name: "/usr/bin/rg")
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        build_fs_tools(WorkspaceRoot.from_path(tmp_path))
    assert not [w for w in caught if "ripgrep" in str(w.message)]


