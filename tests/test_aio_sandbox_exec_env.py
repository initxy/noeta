"""``AioSandboxExecEnv`` — the AIO Sandbox HTTP backend for the fs/shell seam.

These pin the *wire contract* the adapter is coded against (the R2 isolation
layer): given a fake transport, every ``ExecEnv`` method must POST the right
endpoint + body and parse the documented response shape. They never open a
socket — the real container round-trip is a separate, gated
(``NOETA_TEST_AIO_SANDBOX_URL``) end-to-end check. If the live v1 API differs,
these tests are what re-pin the one-file adapter change.
"""

from __future__ import annotations

import base64
import json
from collections.abc import Sequence
from pathlib import Path
from typing import Any, Mapping

import pytest

from noeta.tools.fs.exec_env import (
    AioSandboxError,
    AioSandboxExecEnv,
    ExclusiveCreateExists,
    ExclusiveCreateWriteFailed,
)


BASE = "http://sandbox.local:8080"


class FakeAio:
    """A scripted AIO transport that records every POST.

    ``responses`` maps a URL *path* to either a single response dict or a list
    consumed in order (so a two-call method like ``create_exclusive`` can script
    the gate then the write). Each response dict is the AIO envelope
    (``{"success": ..., "data": {...}}``) returned as JSON bytes.
    """

    def __init__(self, responses: dict[str, Any]) -> None:
        self._responses = {
            path: (val if isinstance(val, list) else [val])
            for path, val in responses.items()
        }
        self.calls: list[tuple[str, dict[str, Any], Mapping[str, str]]] = []
        #: socket read timeout the adapter resolved for each POST, in order.
        self.timeouts: list[float] = []

    def __call__(
        self,
        url: str,
        body: bytes,
        headers: Mapping[str, str],
        *,
        timeout_s: float,
    ) -> bytes:
        path = url[len(BASE):]
        parsed = json.loads(body.decode("utf-8"))
        self.calls.append((path, parsed, headers))
        self.timeouts.append(timeout_s)
        queue = self._responses.get(path)
        if not queue:
            raise AssertionError(f"unexpected call to {path!r}")
        envelope = queue.pop(0)
        return json.dumps(envelope).encode("utf-8")


def _ok(data: dict[str, Any]) -> dict[str, Any]:
    return {"success": True, "message": "ok", "data": data}


def _exec_ok(*, exit_code: int = 0, output: str = "") -> dict[str, Any]:
    return _ok({"session_id": "s1", "status": "completed",
                "exit_code": exit_code, "output": output})


def _env(fake: FakeAio, **kw: Any) -> AioSandboxExecEnv:
    return AioSandboxExecEnv(base_url=BASE, post=fake, **kw)


# -- run_argv --------------------------------------------------------------- #


def test_run_argv_sends_cd_prefixed_shell_quoted_command() -> None:
    fake = FakeAio({"/v1/shell/exec": _exec_ok(exit_code=0, output="hello\n")})
    env = _env(fake)
    outcome = env.run_argv(
        ["echo", "hello world"], cwd=Path("/work/dir"), timeout_s=30, output_cap=1000
    )
    path, body, _ = fake.calls[0]
    assert path == "/v1/shell/exec"
    # cwd is expressed lexically; argv is shell-quoted so the remote re-runs it.
    assert body["command"] == "cd /work/dir && echo 'hello world'"
    assert outcome.returncode == 0
    assert outcome.stdout == b"hello\n"
    assert outcome.stderr == b""  # AIO merges streams into stdout
    assert outcome.timed_out is False


def test_run_argv_prepends_host_preamble_minted_from_argv() -> None:
    # The host preamble (per-session shell setup, minted fresh each call) is
    # inserted verbatim between the ``cd`` and the command, and receives the
    # argv so the host can tailor the prefix to what is being run.
    seen: list[list[str]] = []

    def preamble(argv: Sequence[str]) -> str:
        seen.append(list(argv))
        return "export TOKEN=abc && " if argv and argv[0] == "lark-cli" else ""

    fake = FakeAio({"/v1/shell/exec": _exec_ok(output="ok")})
    _env(fake, preamble=preamble).run_argv(
        ["lark-cli", "whoami"], cwd=Path("/w"), timeout_s=5, output_cap=99
    )
    _, body, _ = fake.calls[0]
    assert body["command"] == "cd /w && export TOKEN=abc && lark-cli whoami"
    assert seen == [["lark-cli", "whoami"]]  # invoked with the real argv


def test_run_argv_preamble_returning_empty_is_byte_identical() -> None:
    # A preamble that returns "" (its no-op / degraded path) leaves the command
    # wire byte-identical to the no-preamble path — the freshness hook must never
    # perturb a stable-prefix recording.
    fake = FakeAio({"/v1/shell/exec": _exec_ok(output="ok")})
    _env(fake, preamble=lambda argv: "").run_argv(
        ["echo", "hi"], cwd=Path("/w"), timeout_s=5, output_cap=99
    )
    _, body, _ = fake.calls[0]
    assert body["command"] == "cd /w && echo hi"


def test_run_argv_propagates_nonzero_exit_and_output() -> None:
    fake = FakeAio({"/v1/shell/exec": _exec_ok(exit_code=2, output="boom")})
    outcome = _env(fake).run_argv(["false"], cwd=Path("/w"), timeout_s=5, output_cap=1000)
    assert outcome.returncode == 2
    assert outcome.stdout == b"boom"


def test_run_argv_caps_output() -> None:
    fake = FakeAio({"/v1/shell/exec": _exec_ok(output="X" * 100)})
    outcome = _env(fake).run_argv(["cat"], cwd=Path("/w"), timeout_s=5, output_cap=10)
    assert outcome.stdout == b"X" * 10
    assert outcome.stdout_truncated is True


def test_run_argv_remote_fault_is_a_reported_failed_run_not_a_raise() -> None:
    fake = FakeAio({"/v1/shell/exec": {"success": False, "message": "down",
                                       "data": {}}})
    outcome = _env(fake).run_argv(["x"], cwd=Path("/w"), timeout_s=5, output_cap=99)
    assert outcome.returncode == -1
    assert b"down" in outcome.stderr


def test_run_argv_threads_caller_budget_as_socket_timeout() -> None:
    # v1 has no remote hard-kill, so the transport read timeout IS the bound:
    # the caller's per-command budget must reach the transport, not a fixed
    # adapter constant. (P1: previously ``timeout_s`` was dropped.)
    fake = FakeAio({"/v1/shell/exec": _exec_ok(output="ok")})
    _env(fake, timeout_s=60.0).run_argv(
        ["sleep", "1"], cwd=Path("/w"), timeout_s=300, output_cap=99
    )
    assert fake.timeouts == [300]


def test_non_shell_ops_use_the_adapter_default_timeout() -> None:
    # File/stat ops carry no per-command budget, so they keep the adapter
    # default rather than borrowing a shell command's timeout.
    fake = FakeAio({"/v1/file/read": _ok({"content": ""})})
    _env(fake, timeout_s=45.0).read_bytes(Path("/w/f"))
    assert fake.timeouts == [45.0]


# -- reads ------------------------------------------------------------------ #


def test_read_bytes_requests_base64_and_decodes() -> None:
    payload = b"\x00\x01binary\xff"
    fake = FakeAio({"/v1/file/read": _ok(
        {"content": base64.b64encode(payload).decode("ascii"), "line_count": 1})})
    env = _env(fake)
    assert env.read_bytes(Path("/w/f.bin")) == payload
    path, body, _ = fake.calls[0]
    assert path == "/v1/file/read"
    assert body == {"file": "/w/f.bin", "encoding": "base64"}


def test_read_text_decodes_utf8() -> None:
    fake = FakeAio({"/v1/file/read": _ok(
        {"content": base64.b64encode("héllo".encode()).decode("ascii")})})
    assert _env(fake).read_text(Path("/w/f.txt")) == "héllo"


def test_read_missing_maps_to_filenotfounderror() -> None:
    fake = FakeAio({"/v1/file/read": {"success": False, "message": "no file",
                                      "data": {"error_type": "not_found"}}})
    with pytest.raises(FileNotFoundError):
        _env(fake).read_bytes(Path("/w/missing"))


# -- writes ----------------------------------------------------------------- #


def test_write_bytes_sends_base64() -> None:
    fake = FakeAio({"/v1/file/write": _ok({})})
    body = b"data\x00here"
    _env(fake).write_bytes(Path("/w/out.bin"), body)
    _, req, _ = fake.calls[0]
    assert req["file"] == "/w/out.bin"
    assert req["encoding"] == "base64"
    assert base64.b64decode(req["content"]) == body


def test_write_permission_denied_maps_to_permissionerror() -> None:
    fake = FakeAio({"/v1/file/write": {"success": False, "message": "denied",
                                       "data": {"error_type": "permission_denied"}}})
    with pytest.raises(PermissionError):
        _env(fake).write_bytes(Path("/w/x"), b"y")


# -- create_exclusive ------------------------------------------------------- #


def test_create_exclusive_gate_then_write() -> None:
    fake = FakeAio({
        "/v1/shell/exec": [_exec_ok(exit_code=0)],  # noclobber gate wins
        "/v1/file/write": [_ok({})],
    })
    body = b"new\n"
    _env(fake).create_exclusive(Path("/w/new.py"), body)
    gate_path, gate_body, _ = fake.calls[0]
    assert gate_path == "/v1/shell/exec"
    assert gate_body["command"] == "set -C; : > /w/new.py"
    write_path, write_body, _ = fake.calls[1]
    assert write_path == "/v1/file/write"
    assert base64.b64decode(write_body["content"]) == body


def test_create_exclusive_collision_raises_exists_no_write() -> None:
    fake = FakeAio({"/v1/shell/exec": _exec_ok(exit_code=1)})  # gate loses
    with pytest.raises(ExclusiveCreateExists) as ei:
        _env(fake).create_exclusive(Path("/w/dup.py"), b"x")
    assert ei.value.recover == "none"
    # no /v1/file/write was attempted
    assert all(c[0] != "/v1/file/write" for c in fake.calls)


def test_create_exclusive_write_failure_is_delete_recovery() -> None:
    fake = FakeAio({
        "/v1/shell/exec": [_exec_ok(exit_code=0)],
        "/v1/file/write": [{"success": False, "message": "disk full",
                            "data": {"error_type": "no_space_left"}}],
    })
    with pytest.raises(ExclusiveCreateWriteFailed) as ei:
        _env(fake).create_exclusive(Path("/w/f.py"), b"x")
    assert ei.value.recover == "delete"


# -- unlink + stat ---------------------------------------------------------- #


def test_unlink_runs_rm() -> None:
    fake = FakeAio({"/v1/shell/exec": _exec_ok(exit_code=0)})
    _env(fake).unlink(Path("/w/gone.txt"))
    _, body, _ = fake.calls[0]
    assert body["command"] == "rm -- /w/gone.txt"


def test_unlink_failure_raises() -> None:
    fake = FakeAio({"/v1/shell/exec": _exec_ok(exit_code=1, output="no such file")})
    with pytest.raises(AioSandboxError):
        _env(fake).unlink(Path("/w/x"))


@pytest.mark.parametrize(
    "method,flag",
    [("exists", "-e"), ("is_file", "-f"), ("is_dir", "-d"), ("is_symlink", "-L")],
)
def test_stat_uses_test_flag(method: str, flag: str) -> None:
    fake = FakeAio({"/v1/shell/exec": _exec_ok(exit_code=0)})
    assert getattr(_env(fake), method)(Path("/w/p")) is True
    _, body, _ = fake.calls[0]
    assert body["command"] == f"test {flag} /w/p"


def test_stat_false_on_nonzero_exit() -> None:
    fake = FakeAio({"/v1/shell/exec": _exec_ok(exit_code=1)})
    assert _env(fake).is_file(Path("/w/p")) is False


# -- glob / rglob ----------------------------------------------------------- #


def test_glob_expands_with_globstar_and_parses_lines() -> None:
    fake = FakeAio({"/v1/shell/exec": _exec_ok(output="/w/a.py\n/w/b.py\n")})
    matches = list(_env(fake).glob(Path("/w"), "*.py"))
    assert matches == [Path("/w/a.py"), Path("/w/b.py")]
    _, body, _ = fake.calls[0]
    assert "shopt -s nullglob dotglob globstar" in body["command"]
    assert body["command"].endswith("printf '%s\\n' /w/*.py")


def test_rglob_is_glob_with_recursive_prefix() -> None:
    fake = FakeAio({"/v1/shell/exec": _exec_ok(output="/w/sub/x.py\n")})
    matches = list(_env(fake).rglob(Path("/w"), "*.py"))
    assert matches == [Path("/w/sub/x.py")]
    _, body, _ = fake.calls[0]
    assert body["command"].endswith("printf '%s\\n' /w/**/*.py")


def test_glob_empty_output_yields_nothing() -> None:
    fake = FakeAio({"/v1/shell/exec": _exec_ok(output="")})
    assert list(_env(fake).glob(Path("/w"), "*.none")) == []


# -- auth + envelope -------------------------------------------------------- #


def test_api_key_rides_as_header() -> None:
    fake = FakeAio({"/v1/file/read": _ok({"content": ""})})
    _env(fake, api_key="secret").read_bytes(Path("/w/f"))
    _, _, headers = fake.calls[0]
    assert headers["X-AIO-API-Key"] == "secret"


def test_no_api_key_sends_no_auth_header() -> None:
    fake = FakeAio({"/v1/file/read": _ok({"content": ""})})
    _env(fake).read_bytes(Path("/w/f"))
    _, _, headers = fake.calls[0]
    assert "X-AIO-API-Key" not in headers


def test_already_exists_error_type_maps_to_fileexistserror() -> None:
    fake = FakeAio({"/v1/file/write": {"success": False, "message": "exists",
                                       "data": {"error_type": "already_exists"}}})
    with pytest.raises(FileExistsError):
        _env(fake).write_bytes(Path("/w/x"), b"y")


def test_unknown_error_type_is_generic_sandbox_error() -> None:
    fake = FakeAio({"/v1/file/read": {"success": False, "message": "weird",
                                      "data": {"error_type": "io_error"}}})
    with pytest.raises(AioSandboxError):
        _env(fake).read_bytes(Path("/w/x"))


def test_empty_base_url_rejected() -> None:
    with pytest.raises(AioSandboxError):
        AioSandboxExecEnv(base_url="")


def test_transport_exception_becomes_sandbox_error() -> None:
    def boom(
        url: str, body: bytes, headers: Mapping[str, str], *, timeout_s: float
    ) -> bytes:
        raise ConnectionError("refused")

    with pytest.raises(AioSandboxError):
        AioSandboxExecEnv(base_url=BASE, post=boom).read_bytes(Path("/w/f"))


def test_fence_token_placeholder_is_accepted() -> None:
    # v1: reserved seam field (D1); accepted, stored, no fence header today.
    fake = FakeAio({"/v1/file/read": _ok({"content": ""})})
    env = _env(fake, fence_token=None)
    env.read_bytes(Path("/w/f"))
    _, _, headers = fake.calls[0]
    assert not any(h.lower().startswith("x-aio-fence") for h in headers)
