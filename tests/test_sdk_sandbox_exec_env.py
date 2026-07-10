"""``SdkSandboxExecEnv`` — the ``agent-sandbox`` SDK fs/shell backend.

These pin the mapping the adapter is coded against: given a fake SDK client,
the three transport overrides (``_shell`` / ``read_bytes`` / ``write_bytes``)
must issue the right ``shell.exec_command`` / ``file.download_file`` /
``file.write_file`` call and map the typed result back to the exact shapes the
inherited :class:`AioSandboxExecEnv` methods parse (so every higher-level method
stays byte-identical). They never open a socket; the live-container round-trip is
exercised separately. Mirrors ``test_aio_sandbox_exec_env.py``.
"""

from __future__ import annotations

import base64
from pathlib import Path
from typing import Any, Callable, Optional

import httpx
import pytest
from agent_sandbox.core.api_error import ApiError

from noeta.agent.host.sdk_sandbox_exec_env import SdkSandboxExecEnv
from noeta.tools.fs.exec_env import (
    AioSandboxError,
    ExclusiveCreateExists,
)


BASE = "http://sandbox.local:8080"


class _Data:
    """A stand-in for the SDK's typed ``.data`` model (``extra='allow'``)."""

    def __init__(self, **kw: Any) -> None:
        self.__dict__.update(kw)


class _Resp:
    def __init__(
        self,
        data: Any,
        success: Optional[bool] = True,
        message: Optional[str] = None,
    ) -> None:
        self.data = data
        self.success = success
        self.message = message


class FakeShell:
    def __init__(self, script: Callable[[str], Any]) -> None:
        #: ``script`` returns a ``_Data`` (wrapped in a success ``_Resp``) or a
        #: full ``_Resp`` (to script ``success=false`` replies).
        self._script = script
        self.calls: list[tuple[str, Optional[dict[str, Any]]]] = []

    def exec_command(
        self, *, command: str, request_options: Optional[dict[str, Any]] = None
    ) -> _Resp:
        self.calls.append((command, request_options))
        result = self._script(command)
        return result if isinstance(result, _Resp) else _Resp(result)


class FakeFile:
    def __init__(
        self,
        reads: Optional[dict[str, bytes]] = None,
        errors: Optional[dict[str, Exception]] = None,
        write_response: Optional[_Resp] = None,
    ) -> None:
        self.reads = reads or {}
        self.errors = errors or {}
        self.write_response = write_response
        self.writes: list[tuple[str, str, Optional[str]]] = []

    def download_file(
        self, *, path: str, request_options: Optional[dict[str, Any]] = None
    ):
        if path in self.errors:
            raise self.errors[path]
        # Stream in two chunks to exercise the ``b"".join`` reassembly.
        blob = self.reads.get(path, b"")
        yield blob[: len(blob) // 2]
        yield blob[len(blob) // 2 :]

    def write_file(
        self,
        *,
        file: str,
        content: str,
        encoding: Optional[str] = None,
        request_options: Optional[dict[str, Any]] = None,
    ) -> _Resp:
        self.writes.append((file, content, encoding))
        if self.write_response is not None:
            return self.write_response
        return _Resp(_Data(file=file, bytes_written=len(content)))


class FakeSandbox:
    def __init__(self, shell: FakeShell, file: FakeFile) -> None:
        self.shell = shell
        self.file = file


def _exec_ok(
    *, exit_code: int = 0, output: str = "", full_output_file_path: Optional[str] = None
) -> _Data:
    data = _Data(session_id="s1", status="completed", exit_code=exit_code, output=output)
    if full_output_file_path is not None:
        data.full_output_file_path = full_output_file_path
    return data


def _env(
    *,
    shell: Optional[FakeShell] = None,
    file: Optional[FakeFile] = None,
    **kw: Any,
) -> tuple[SdkSandboxExecEnv, FakeShell, FakeFile]:
    shell = shell or FakeShell(lambda cmd: _exec_ok())
    file = file or FakeFile()
    env = SdkSandboxExecEnv(base_url=BASE, client=FakeSandbox(shell, file), **kw)
    return env, shell, file


# -- construction ----------------------------------------------------------- #


def test_empty_base_url_raises_at_construction() -> None:
    with pytest.raises(AioSandboxError):
        SdkSandboxExecEnv(base_url="", client=FakeSandbox(FakeShell(lambda c: _exec_ok()), FakeFile()))


# -- run_argv (inherited, driven through the _shell override) --------------- #


def test_run_argv_sends_cd_prefixed_shell_quoted_command() -> None:
    env, shell, _ = _env(shell=FakeShell(lambda cmd: _exec_ok(exit_code=0, output="hello\n")))
    outcome = env.run_argv(
        ["echo", "hello world"], cwd=Path("/work/dir"), timeout_s=30, output_cap=1000
    )
    command, request_options = shell.calls[0]
    assert command == "cd /work/dir && echo 'hello world'"
    # the caller's per-command budget rides as the SDK per-call timeout
    assert request_options == {"timeout_in_seconds": 30}
    assert outcome.returncode == 0
    assert outcome.stdout == b"hello\n"
    assert outcome.stderr == b""  # AIO merges streams into stdout
    assert outcome.timed_out is False


def test_run_argv_propagates_nonzero_exit() -> None:
    env, _, _ = _env(shell=FakeShell(lambda cmd: _exec_ok(exit_code=7, output="boom")))
    outcome = env.run_argv(["false"], cwd=Path("/w"), timeout_s=5, output_cap=100)
    assert outcome.returncode == 7
    assert outcome.stdout == b"boom"


def test_shell_omits_missing_exit_code_so_consumer_defaults_apply() -> None:
    # An absent exit code means the command did NOT complete (status running /
    # timeout / terminated). ``_shell`` must pass that absence through — the
    # urllib wire's raw-dict semantics — so each inherited consumer applies its
    # own default: stat treats it as failure (False), ``run_argv`` keeps 0.
    env, _, _ = _env(
        shell=FakeShell(lambda cmd: _Data(status="running", output="x", exit_code=None))
    )
    assert env.is_file(Path("/a")) is False
    assert env.exists(Path("/a")) is False
    outcome = env.run_argv(["true"], cwd=Path("/w"), timeout_s=5, output_cap=100)
    assert outcome.returncode == 0


def test_shell_success_false_raises_with_server_message() -> None:
    # The v1 wire signals in-band failure as ``200 + success:false``; the Fern
    # client parses it without raising, so the adapter must surface it.
    env, _, _ = _env(
        shell=FakeShell(lambda cmd: _Resp(None, success=False, message="shell down"))
    )
    with pytest.raises(AioSandboxError, match="shell down"):
        env.unlink(Path("/x"))


def test_run_argv_timeout_maps_to_timed_out_run() -> None:
    def slow(cmd: str) -> _Data:
        raise httpx.ReadTimeout("read deadline")

    env, _, _ = _env(shell=FakeShell(slow))
    outcome = env.run_argv(["sleep", "99"], cwd=Path("/w"), timeout_s=1, output_cap=100)
    assert outcome.timed_out is True
    assert outcome.returncode == -1


def test_run_argv_recovers_spilled_output_tail() -> None:
    # A truncated inline echo with ``full_output_file_path`` must be recovered
    # via ``tail -c`` (the inherited ``_read_spill``), not the lossy inline echo.
    def script(cmd: str) -> _Data:
        if cmd.startswith("tail -c"):
            return _exec_ok(output="full-spilled-output")
        return _exec_ok(output="truncated", full_output_file_path="/tmp/spill.log")

    env, shell, _ = _env(shell=FakeShell(script))
    outcome = env.run_argv(["make"], cwd=Path("/w"), timeout_s=30, output_cap=1000)
    assert outcome.stdout == b"full-spilled-output"
    assert any(cmd == "tail -c 1001 -- /tmp/spill.log" for cmd, _ in shell.calls)


def test_spill_field_survives_the_typed_sdk_model() -> None:
    # ``full_output_file_path`` is NOT a declared field of the SDK's
    # ``ShellCommandResult``; the adapter reads it via ``getattr``, which only
    # works because the Fern models are ``extra="allow"``. Pin that against the
    # real model so an SDK bump that drops the extra silently fails here.
    from agent_sandbox.types.shell_command_result import ShellCommandResult

    model = ShellCommandResult.model_validate(
        {
            "session_id": "s",
            "command": "c",
            "status": "completed",
            "output": "x",
            "exit_code": 0,
            "full_output_file_path": "/tmp/spill.log",
        }
    )
    assert getattr(model, "full_output_file_path", None) == "/tmp/spill.log"


def test_preamble_is_prepended_fresh_each_exec() -> None:
    env, shell, _ = _env(preamble=lambda argv: "export T=1 && ")
    env.run_argv(["echo", "x"], cwd=Path("/w"), timeout_s=5, output_cap=100)
    assert shell.calls[0][0] == "cd /w && export T=1 && echo x"


def test_run_argv_reports_apierror_as_failed_run() -> None:
    def boom(cmd: str) -> _Data:
        raise ApiError(status_code=500, headers={}, body={"message": "kaboom"})

    env, _, _ = _env(shell=FakeShell(boom))
    outcome = env.run_argv(["x"], cwd=Path("/w"), timeout_s=5, output_cap=100)
    assert outcome.returncode == -1
    assert outcome.timed_out is False
    assert b"kaboom" in outcome.stderr


# -- read_bytes (download_file) --------------------------------------------- #


def test_read_bytes_joins_download_stream_byte_exact() -> None:
    payload = bytes(range(256))
    env, _, _ = _env(file=FakeFile(reads={"/f.bin": payload}))
    assert env.read_bytes(Path("/f.bin")) == payload


def test_read_text_decodes_utf8() -> None:
    env, _, _ = _env(file=FakeFile(reads={"/u.txt": "héllo 世界".encode()}))
    assert env.read_text(Path("/u.txt")) == "héllo 世界"


def test_read_missing_maps_to_filenotfounderror() -> None:
    err = ApiError(status_code=404, headers={}, body={"message": "no such file"})
    env, _, _ = _env(file=FakeFile(errors={"/missing": err}))
    with pytest.raises(FileNotFoundError):
        env.read_bytes(Path("/missing"))


def test_read_permission_denied_maps_to_permissionerror() -> None:
    err = ApiError(status_code=403, headers={}, body={"message": "denied"})
    env, _, _ = _env(file=FakeFile(errors={"/x": err}))
    with pytest.raises(PermissionError):
        env.read_bytes(Path("/x"))


# -- write_bytes ------------------------------------------------------------ #


def test_write_bytes_sends_base64() -> None:
    env, _, file = _env()
    env.write_bytes(Path("/out.bin"), b"\x00\x01\x02payload")
    file_arg, content, encoding = file.writes[0]
    assert file_arg == "/out.bin"
    assert encoding == "base64"
    assert base64.b64decode(content) == b"\x00\x01\x02payload"


def test_write_success_false_maps_error_type() -> None:
    # In-band failure on write (200 + success:false + data.error_type) — the
    # old wire's primary failure channel; must not be silently dropped.
    resp = _Resp(_Data(error_type="permission_denied"), success=False, message="denied")
    env, _, _ = _env(file=FakeFile(write_response=resp))
    with pytest.raises(PermissionError, match="denied"):
        env.write_bytes(Path("/ro/x"), b"body")


def test_write_success_false_without_error_type_degrades() -> None:
    resp = _Resp(None, success=False, message="disk full")
    env, _, _ = _env(file=FakeFile(write_response=resp))
    with pytest.raises(AioSandboxError, match="disk full"):
        env.write_bytes(Path("/x"), b"body")


def test_read_bytes_over_total_cap_raises() -> None:
    # Parity with the urllib backend's response cap: a huge container file must
    # raise a clean error, not be reassembled without bound.
    env, _, _ = _env(file=FakeFile(reads={"/big": b"x" * 100}), total_cap=64)
    with pytest.raises(AioSandboxError, match="total cap"):
        env.read_bytes(Path("/big"))


# -- create_exclusive / tree_snapshot (inherited, via _shell + file ops) ----- #


def test_create_exclusive_gates_then_writes() -> None:
    env, shell, file = _env()
    env.create_exclusive(Path("/n.txt"), b"body")
    assert shell.calls[0][0] == "set -C; : > /n.txt"
    assert file.writes[0][0] == "/n.txt"
    assert base64.b64decode(file.writes[0][1]) == b"body"


def test_create_exclusive_existing_path_raises_exists() -> None:
    env, _, _ = _env(shell=FakeShell(lambda cmd: _exec_ok(exit_code=1)))
    with pytest.raises(ExclusiveCreateExists):
        env.create_exclusive(Path("/taken.txt"), b"body")


def test_create_exclusive_indeterminate_gate_raises_exists() -> None:
    # The noclobber gate is a safety branch: a command that did not complete
    # (no exit code) must NOT read as "gate opened" — else an existing file
    # could be overwritten.
    env, _, _ = _env(
        shell=FakeShell(lambda cmd: _Data(status="running", output="", exit_code=None))
    )
    with pytest.raises(ExclusiveCreateExists):
        env.create_exclusive(Path("/racy.txt"), b"body")


def test_tree_snapshot_parses_listing_and_inlines_contents() -> None:
    def b64(raw: bytes) -> str:
        return base64.b64encode(raw).decode("ascii")

    listing = (
        f"F {b64(b'/r/a.txt')}\n"
        f"C {b64(b'/r/SKILL.md')} {b64(b'skill body')}\n"
    )
    env, shell, _ = _env(shell=FakeShell(lambda cmd: _exec_ok(output=listing)))
    snap = env.tree_snapshot([Path("/r")], content_name="SKILL.md")
    assert snap.files == (Path("/r/SKILL.md"), Path("/r/a.txt"))
    assert snap.contents == {Path("/r/SKILL.md"): b"skill body"}
    assert shell.calls[0][0].startswith("find -L /r -type f")


# -- close ------------------------------------------------------------------- #


def test_close_is_noop_for_injected_client() -> None:
    env, _, _ = _env()
    env.close()  # owned pool is None with an injected client — must not raise
    env.close()  # idempotent


# -- stat / glob (inherited, driven through _shell) ------------------------- #


def test_glob_expands_and_parses_lines() -> None:
    env, shell, _ = _env(
        shell=FakeShell(lambda cmd: _exec_ok(output="/base/a.txt\n/base/b.txt\n"))
    )
    got = sorted(str(p) for p in env.glob(Path("/base"), "*.txt"))
    assert got == ["/base/a.txt", "/base/b.txt"]
    assert "globstar" in shell.calls[0][0]  # inherited command shape unchanged


def test_auth_headers_ride_as_per_call_request_option() -> None:
    env, shell, _ = _env(auth_headers=lambda: {"X-AIO-API-Key": "secret"})
    env.run_argv(["echo", "x"], cwd=Path("/w"), timeout_s=10, output_cap=100)
    _, request_options = shell.calls[0]
    assert request_options is not None
    assert request_options["additional_headers"] == {"X-AIO-API-Key": "secret"}
