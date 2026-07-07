"""``ExecEnv`` — the execution-backend seam under the fs / shell tool pack.

Every fs / shell tool performs two kinds of side effect against the
workspace: **file IO** (read / write / stat / directory walk) and
**process execution** (spawn a command). Today both go straight to the
local host — ``Path.read_bytes`` / ``Path.glob`` / ``subprocess`` via
``run_argv``. ``ExecEnv`` is the one seam those leaf operations route
through, so the *same* tool code can run either against the local host
(:class:`LocalExecEnv`, the default — byte-identical to the pre-seam
behaviour) or against a remote sandbox container (a later
``AioSandboxExecEnv`` that satisfies this same Protocol over HTTP).

Deliberately **IO-only**. Path *resolution* (the ``WorkspaceRoot``
containment fence — ``resolve`` / ``resolve_readable`` / ``relative`` /
``root``) stays on ``WorkspaceRoot`` and is unchanged: a tool still
resolves a user-supplied path to an absolute :class:`~pathlib.Path`
through the fence, then hands that *resolved* path to the ``ExecEnv`` for
the actual read / write / walk. For a remote backend the ``WorkspaceRoot``
is simply rooted at the container's workspace path (lexical containment —
absolute / ``..`` escapes still rejected; the container itself is the
real isolation boundary), and the ``ExecEnv`` IO methods are what cross
the process boundary.

:class:`LocalExecEnv` is **stateless**: it operates on the absolute
``Path`` the tool already resolved, so a tool passing ``resolved`` to
``exec_env.read_bytes(resolved)`` produces the exact same bytes as the
old ``resolved.read_bytes()``. That byte-identity is the contract this
seam is introduced under (Noeta's stable-prefix / resume moat): swapping
the *executor* must never perturb a tool's recorded output.
"""

from __future__ import annotations

import base64
import contextlib
import json
import os
import shlex
import subprocess
import time
import urllib.error
import urllib.request
from collections.abc import Iterable
from pathlib import Path
from typing import Any, Callable, Mapping, Optional, Protocol, runtime_checkable

from noeta.tools.fs._subprocess import (
    _RunOutcome,
    cap_stream,
    run_argv as _local_run_argv,
)


__all__ = [
    "AioSandboxError",
    "AioSandboxExecEnv",
    "AioHttpPost",
    "DEFAULT_AIO_TIMEOUT_S",
    "ExclusiveCreateError",
    "ExclusiveCreateExists",
    "ExclusiveCreateFailed",
    "ExclusiveCreateWriteFailed",
    "ExecEnv",
    "LocalExecEnv",
]


def _write_all(fd: int, data: bytes) -> None:
    """Write every byte to ``fd`` (``os.write`` may short-write).

    A zero-length write or an ``OSError`` is a failure — a partial write is
    NEVER treated as success. Moved here from ``patch.py`` when
    ``apply_patch``'s exclusive-create routed through
    :meth:`ExecEnv.create_exclusive`; the ``os.write`` seam the patch tests
    monkeypatch is unchanged.
    """
    mv = memoryview(data)
    total = 0
    while total < len(data):
        n = os.write(fd, mv[total:])
        if n <= 0:
            raise OSError("short write (os.write returned 0)")
        total += n


class ExclusiveCreateError(OSError):
    """A :meth:`ExecEnv.create_exclusive` failure, carrying the rollback verb.

    ``apply_patch`` distinguishes three outcomes of an atomic create so it can
    pick the right recovery (``recover="none"`` when the target was never
    created by us, ``recover="delete"`` when it was created then the write /
    close failed). Subclasses fix ``recover``; ``reason`` is the exact
    human-facing message the tool surfaces (preserved byte-for-byte from the
    pre-seam inline dance). Subclasses ``OSError`` so the tool's existing
    ``except OSError`` rollback sites keep working.
    """

    #: The rollback verb ``apply_patch._fail`` acts on ("none" | "delete").
    recover: str = "none"

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        #: The exact failure message the tool surfaces as its ``reason``.
        self.reason = reason


class ExclusiveCreateExists(ExclusiveCreateError):
    """The exclusive ``O_EXCL`` open failed because the path already exists —
    the target was NOT created by this call, so ``recover="none"``."""

    recover = "none"


class ExclusiveCreateFailed(ExclusiveCreateError):
    """The exclusive open failed for a non-existence reason (permissions,
    missing parent, …) — nothing created, so ``recover="none"``."""

    recover = "none"


class ExclusiveCreateWriteFailed(ExclusiveCreateError):
    """The open SUCCEEDED but the subsequent write / close failed — the file
    now EXISTS and must be deleted, so ``recover="delete"``."""

    recover = "delete"

#: The injectable subprocess runner ``shell_run`` threads through to
#: ``run_argv`` (tests pass a fake to avoid shelling out on the happy path).
#: A ``subprocess.run``-shaped callable; ``None`` ⇒ the default local runner.
_SubprocRunner = Callable[..., "subprocess.CompletedProcess[bytes]"]


@runtime_checkable
class ExecEnv(Protocol):
    """The file-IO + process-execution backend a fs / shell tool acts through.

    Every method operates on an **already-resolved absolute path** (the tool
    passes the ``Path`` it obtained from ``WorkspaceRoot.resolve`` /
    ``resolve_readable``); the ``ExecEnv`` never does containment itself. A
    ``LocalExecEnv`` reads the host filesystem; a sandbox backend fulfils the
    same shape against a container over its API.
    """

    # -- file reads --------------------------------------------------------
    def read_bytes(self, path: Path) -> bytes: ...

    def read_text(self, path: Path, *, encoding: str = "utf-8") -> str: ...

    # -- file writes -------------------------------------------------------
    def write_bytes(self, path: Path, body: bytes) -> None: ...

    def create_exclusive(self, path: Path, body: bytes) -> None:
        """Atomically create ``path`` and write ``body`` — never overwrite.

        The create must fail if the path already exists. On failure raises a
        :class:`ExclusiveCreateError` subclass whose ``recover`` /``reason``
        tell ``apply_patch`` how to roll back (the seam preserves the exact
        recovery semantics ``apply_patch`` had inline before the seam existed).
        """
        ...

    def unlink(self, path: Path) -> None: ...

    def mkdir(self, path: Path) -> None:
        """Create ``path`` and any missing parents (``parents=True,
        exist_ok=True``). Used by the rewind restore (T7) to re-create a
        directory an edited-then-restored file lived in when the rewound span
        had removed it. An existing directory is not an error."""
        ...

    # -- stat --------------------------------------------------------------
    def exists(self, path: Path) -> bool: ...

    def is_file(self, path: Path) -> bool: ...

    def is_dir(self, path: Path) -> bool: ...

    def is_symlink(self, path: Path) -> bool: ...

    # -- directory walk ----------------------------------------------------
    def glob(self, base: Path, pattern: str) -> Iterable[Path]:
        """``base.glob(pattern)`` — one directory level of pattern expansion."""
        ...

    def rglob(self, base: Path, pattern: str) -> Iterable[Path]:
        """``base.rglob(pattern)`` — recursive pattern expansion."""
        ...

    # -- process -----------------------------------------------------------
    @property
    def supports_background(self) -> bool:
        """Whether ``shell_run(run_in_background=True)`` is valid on this backend.

        The host background runner (``ProcessRegistry``) spawns detached HOST
        subprocesses — it cannot reach into a container, and AIO exposes no
        durable job handle (a v2 concern). So a container backend returns
        ``False`` and ``shell_run`` refuses a background launch cleanly; the
        local host returns ``True``."""
        ...

    def run_argv(
        self,
        argv: list[str],
        *,
        cwd: Path,
        timeout_s: int,
        output_cap: int,
        runner: Optional[_SubprocRunner] = None,
    ) -> _RunOutcome:
        """Spawn ``argv`` under ``cwd``, capture output, enforce timeout + cap.

        Returns the same :class:`~noeta.tools.fs._subprocess._RunOutcome` the
        tools already consume.
        """
        ...


class LocalExecEnv:
    """The default :class:`ExecEnv`: the local host filesystem + subprocess.

    Stateless — every method is the exact ``Path`` / ``os`` / ``run_argv``
    operation the tools performed inline before the seam existed, so a tool
    routed through ``LocalExecEnv`` records byte-identical output. One shared
    instance is safe to reuse across every tool and task.
    """

    __slots__ = ()

    def read_bytes(self, path: Path) -> bytes:
        return path.read_bytes()

    def read_text(self, path: Path, *, encoding: str = "utf-8") -> str:
        return path.read_text(encoding=encoding)

    def write_bytes(self, path: Path, body: bytes) -> None:
        path.write_bytes(body)

    def create_exclusive(self, path: Path, body: bytes) -> None:
        # The exact fd-level dance apply_patch ran inline before the seam
        # existed: exclusive O_EXCL open, write-all, close — each failure
        # mapped to the recovery verb the tool expects. The ``os.open`` /
        # ``os.write`` / ``os.close`` seams the patch tests monkeypatch are
        # unchanged; only their home moved.
        try:
            fd = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
        except FileExistsError as exc:
            raise ExclusiveCreateExists(
                "path created by another process (exclusive create)"
            ) from exc
        except OSError as exc:
            raise ExclusiveCreateFailed(f"create failed: {exc}") from exc
        # The file now EXISTS — any failure (write OR close) must delete it;
        # a close OSError must NOT escape and bypass rollback (close can report
        # a deferred write-back error).
        try:
            _write_all(fd, body)
        except OSError as exc:
            with contextlib.suppress(OSError):
                os.close(fd)
            raise ExclusiveCreateWriteFailed(f"write failed: {exc}") from exc
        try:
            os.close(fd)
        except OSError as exc:
            raise ExclusiveCreateWriteFailed(f"close failed: {exc}") from exc

    def unlink(self, path: Path) -> None:
        path.unlink()

    def mkdir(self, path: Path) -> None:
        path.mkdir(parents=True, exist_ok=True)

    @property
    def supports_background(self) -> bool:
        # The host runner spawns host subprocesses — valid for the local host.
        return True

    def exists(self, path: Path) -> bool:
        return path.exists()

    def is_file(self, path: Path) -> bool:
        return path.is_file()

    def is_dir(self, path: Path) -> bool:
        return path.is_dir()

    def is_symlink(self, path: Path) -> bool:
        return path.is_symlink()

    def glob(self, base: Path, pattern: str) -> Iterable[Path]:
        return base.glob(pattern)

    def rglob(self, base: Path, pattern: str) -> Iterable[Path]:
        return base.rglob(pattern)

    def run_argv(
        self,
        argv: list[str],
        *,
        cwd: Path,
        timeout_s: int,
        output_cap: int,
        runner: Optional[_SubprocRunner] = None,
    ) -> _RunOutcome:
        return _local_run_argv(
            argv,
            cwd=cwd,
            timeout_s=timeout_s,
            output_cap=output_cap,
            runner=runner,
        )


# --------------------------------------------------------------------------- #
# AIO Sandbox backend
# --------------------------------------------------------------------------- #

#: Default per-call HTTP timeout for a sandbox request (seconds). ``run_argv``
#: overrides it per call from the tool's own timeout budget.
DEFAULT_AIO_TIMEOUT_S = 60.0

#: Cap on a single response body (bytes) — bounds memory the same way the MCP
#: HTTP client does.
_DEFAULT_AIO_TOTAL_CAP = 32 * 1024 * 1024

#: Injectable HTTP transport for the AIO backend: ``(url, json_body, headers)``
#: → raw response body bytes. Injectable so tests substitute a fake and never
#: shell out / open a socket (the ``mcp_http_post`` / ``otlp_http_post``
#: pattern); production leaves it ``None`` to use stdlib ``urllib``.
AioHttpPost = Callable[[str, bytes, Mapping[str, str]], bytes]


class AioSandboxError(OSError):
    """A failed AIO Sandbox call (transport, protocol, or ``success=false``).

    Subclasses :class:`OSError` so the fs tools' existing ``except OSError``
    sites (read / write / rollback) treat a remote failure exactly like a local
    IO failure. :meth:`AioSandboxExecEnv._call` refines *file* faults into the
    stdlib ``OSError`` subclass the local backend would have raised
    (``FileNotFoundError`` / ``PermissionError`` / ``FileExistsError``) so the
    tools branch identically against either backend.
    """


#: AIO ``data.error_type`` → the stdlib ``OSError`` subclass the local backend
#: raises for the same fault, so tool branching is backend-agnostic.
_AIO_ERROR_TYPES: dict[str, type[OSError]] = {
    "not_found": FileNotFoundError,
    "permission_denied": PermissionError,
    "already_exists": FileExistsError,
}


class AioSandboxExecEnv:
    """:class:`ExecEnv` backed by an AIO Sandbox container over HTTP.

    Every file / process side effect is routed to a single
    ``agent-infra/sandbox`` container's v1 API — ``POST /v1/shell/exec`` for
    process execution and ``POST /v1/file/{read,write}`` for file IO — instead
    of the local host. The tool code above the seam is byte-for-byte the same;
    only the executor changes, so a tool's model-facing contract (and its
    recorded output shape) is unaffected.

    **This is the R2 isolation layer.** The AIO v1 wire contract (field names,
    the base64 read/write encoding, the merged ``output`` stream) is captured
    *only here* and pinned by fake-transport tests; a contract drift is a
    one-file change. The mapping decisions worth calling out:

    * **``run_argv``** joins the argv with :func:`shlex.join` and prefixes
      ``cd <cwd> && `` — cwd is expressed lexically rather than relying on an
      unconfirmed request field. AIO returns a single merged ``output``
      stream, so it lands in ``stdout`` and ``stderr`` is empty (the tool's
      output shape tolerates this — the local backend already merges nothing,
      but nothing downstream requires the split).
    * **byte fidelity** — reads request ``encoding="base64"`` and decode; writes
      send base64. This is the byte-exact path (edit / apply_patch hash the
      bytes for TOCTOU), and it is the single most contract-sensitive field.
    * **``glob`` / ``rglob``** are expressed with shell ``globstar`` (``rglob``
      = ``glob('**/'+pattern)``, matching pathlib's own definition); they
      depend on ``bash`` + ``globstar`` in the image (R5). Their pathlib
      semantics are a best-effort approximation validated only against a live
      container (gated ``NOETA_TEST_AIO_SANDBOX_URL``).
    * **stat** (``exists`` / ``is_file`` / …) runs ``test`` and reads the exit
      code; **``unlink``** runs ``rm``.

    ``fence_token`` is the v1 placeholder for the v2 generation-token fence
    (D1): the seam shape already carries it so v2 can fill it without touching
    this interface; v1 leaves it ``None`` and sends no fence header.
    """

    def __init__(
        self,
        *,
        base_url: str,
        api_key: Optional[str] = None,
        timeout_s: float = DEFAULT_AIO_TIMEOUT_S,
        total_cap: int = _DEFAULT_AIO_TOTAL_CAP,
        post: Optional[AioHttpPost] = None,
        fence_token: Optional[str] = None,
    ) -> None:
        if not base_url:
            raise AioSandboxError("aio sandbox base_url is empty")
        self._base = base_url.rstrip("/")
        self._timeout_s = timeout_s
        self._total_cap = total_cap
        self._post = post or self._default_post
        # v1: fence_token is a reserved placeholder (D1); always None today, so
        # no fence header is sent. v2 rotates it on stale-reclaim.
        self._fence_token = fence_token
        headers = {"Content-Type": "application/json"}
        if api_key:
            # AIO accepts the key via X-AIO-API-Key / Authorization: Bearer /
            # ?api_key=. We use the header form; it rides only on the wire —
            # never recorded (D5).
            headers["X-AIO-API-Key"] = api_key
        self._headers = headers

    # -- wire ------------------------------------------------------------- #

    def _call(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        """POST ``body`` to ``path`` and return the response ``data`` object.

        Raises the mapped :class:`OSError` subclass on ``success=false`` and
        :class:`AioSandboxError` on any transport / protocol fault. The HTTP
        read timeout is the adapter-level ``timeout_s`` (there is no per-call
        override — AIO does not hard-kill a slow ``exec``; the lease heartbeat
        + 1h cap is the real bound, see D1/limitations).
        """
        url = self._base + path
        data = json.dumps(body, separators=(",", ":")).encode("utf-8")
        try:
            raw = self._post(url, data, self._headers)
        except TimeoutError:
            # Propagated raw so run_argv can classify it as a timed-out run;
            # other callers see it as an OSError (a tool_error) all the same.
            raise
        except AioSandboxError:
            raise
        except Exception as exc:  # any other transport fault (ConnectionError,
            # URLError, …) is normalised to our OSError subclass.
            raise AioSandboxError(f"{path}: transport error: {exc}") from exc
        if len(raw) > self._total_cap:
            raise AioSandboxError(f"{path}: response exceeded total cap")
        try:
            obj = json.loads(raw.decode("utf-8", errors="replace"))
        except json.JSONDecodeError as exc:
            raise AioSandboxError(f"{path}: malformed JSON response: {exc}") from exc
        if not isinstance(obj, dict):
            raise AioSandboxError(f"{path}: response is not an object")
        if not obj.get("success"):
            raise self._error(path, obj)
        result = obj.get("data")
        return result if isinstance(result, dict) else {}

    @staticmethod
    def _error(path: str, obj: dict[str, Any]) -> OSError:
        message = obj.get("message") or f"{path}: request failed"
        data = obj.get("data")
        error_type = data.get("error_type") if isinstance(data, dict) else None
        cls = _AIO_ERROR_TYPES.get(error_type or "", AioSandboxError)
        return cls(message)

    def _shell(self, command: str) -> dict[str, Any]:
        return self._call("/v1/shell/exec", {"command": command})

    # -- file reads ------------------------------------------------------- #

    def read_bytes(self, path: Path) -> bytes:
        data = self._call(
            "/v1/file/read", {"file": str(path), "encoding": "base64"}
        )
        content = data.get("content")
        if not isinstance(content, str):
            raise AioSandboxError(f"read {path}: response missing 'content'")
        try:
            return base64.b64decode(content)
        except (ValueError, TypeError) as exc:
            raise AioSandboxError(f"read {path}: bad base64 content: {exc}") from exc

    def read_text(self, path: Path, *, encoding: str = "utf-8") -> str:
        return self.read_bytes(path).decode(encoding)

    # -- file writes ------------------------------------------------------ #

    def write_bytes(self, path: Path, body: bytes) -> None:
        self._call(
            "/v1/file/write",
            {
                "file": str(path),
                "content": base64.b64encode(body).decode("ascii"),
                "encoding": "base64",
            },
        )

    def create_exclusive(self, path: Path, body: bytes) -> None:
        # AIO has no O_EXCL; emulate exclusivity with a noclobber gate so a
        # concurrent create loses the race deterministically. ``set -C`` makes
        # ``>`` fail if the target exists; only on that gate succeeding do we
        # write the real (base64) body. Recovery verbs mirror the local dance:
        # a pre-existing path never became ours (recover="none"); a gate that
        # opened but whose body write failed leaves a file to delete
        # (recover="delete").
        gate = self._shell(f"set -C; : > {shlex.quote(str(path))}")
        if int(gate.get("exit_code", 1)) != 0:
            raise ExclusiveCreateExists(
                "path created by another process (exclusive create)"
            )
        try:
            self.write_bytes(path, body)
        except OSError as exc:
            raise ExclusiveCreateWriteFailed(f"write failed: {exc}") from exc

    def unlink(self, path: Path) -> None:
        outcome = self._shell(f"rm -- {shlex.quote(str(path))}")
        if int(outcome.get("exit_code", 1)) != 0:
            raise AioSandboxError(f"unlink {path}: {outcome.get('output', '')!r}")

    def mkdir(self, path: Path) -> None:
        # ``mkdir -p`` = parents=True, exist_ok=True — the exact restore
        # semantics ``LocalExecEnv.mkdir`` gives on the host.
        outcome = self._shell(f"mkdir -p -- {shlex.quote(str(path))}")
        if int(outcome.get("exit_code", 1)) != 0:
            raise AioSandboxError(f"mkdir {path}: {outcome.get('output', '')!r}")

    @property
    def supports_background(self) -> bool:
        # v1: no container-side durable job handle; the host runner would spawn
        # on the HOST, not the container — so shell_run refuses a background
        # launch cleanly (D5). v2 owns container background as separate work.
        return False

    # -- stat ------------------------------------------------------------- #

    def _test(self, flag: str, path: Path) -> bool:
        outcome = self._shell(f"test {flag} {shlex.quote(str(path))}")
        return int(outcome.get("exit_code", 1)) == 0

    def exists(self, path: Path) -> bool:
        return self._test("-e", path)

    def is_file(self, path: Path) -> bool:
        return self._test("-f", path)

    def is_dir(self, path: Path) -> bool:
        return self._test("-d", path)

    def is_symlink(self, path: Path) -> bool:
        return self._test("-L", path)

    # -- directory walk --------------------------------------------------- #

    def glob(self, base: Path, pattern: str) -> Iterable[Path]:
        # ``bash`` globstar expansion of ``<base>/<pattern>``: nullglob so no
        # match yields nothing (not the literal pattern), dotglob so hidden
        # entries are included (pathlib's glob does too), globstar so ``**``
        # spans directories. ``base`` is quoted; the pattern is intentionally
        # unquoted so the shell expands it (the container is already the
        # trust/isolation boundary — a shell tool can run anything in it).
        command = (
            "shopt -s nullglob dotglob globstar; "
            f"printf '%s\\n' {shlex.quote(str(base))}/{pattern}"
        )
        outcome = self._shell(command)
        text = outcome.get("output") or ""
        return [Path(line) for line in text.splitlines() if line]

    def rglob(self, base: Path, pattern: str) -> Iterable[Path]:
        # pathlib defines rglob(pat) as glob("**/" + pat); mirror it exactly.
        return self.glob(base, "**/" + pattern)

    # -- process ---------------------------------------------------------- #

    def run_argv(
        self,
        argv: list[str],
        *,
        cwd: Path,
        timeout_s: int,
        output_cap: int,
        runner: Optional[_SubprocRunner] = None,
    ) -> _RunOutcome:
        # ``runner`` is the local subprocess seam — irrelevant remotely, ignored.
        # cwd is expressed lexically (cd &&) rather than via an unconfirmed
        # request field; argv is shell-quoted so the remote shell re-runs the
        # exact tokens.
        del runner
        command = f"cd {shlex.quote(str(cwd))} && {shlex.join(argv)}"
        del timeout_s  # v1: no remote hard-kill; the HTTP timeout bounds the call
        start = time.monotonic()
        try:
            data = self._shell(command)
        except TimeoutError as exc:
            # socket.timeout is TimeoutError on 3.10+; a URLError-wrapped
            # timeout instead surfaces as AioSandboxError below (still a
            # reported failed run, not a crash).
            duration_ms = int((time.monotonic() - start) * 1000)
            return _RunOutcome(
                returncode=-1,
                duration_ms=duration_ms,
                stdout=b"",
                stderr=str(exc).encode("utf-8"),
                stdout_truncated=False,
                stderr_truncated=False,
                timed_out=True,
            )
        except AioSandboxError as exc:
            # A remote fault is reported to the model as a failed run rather
            # than crashing the worker, mirroring the local backend which never
            # lets a spawn fault escape run_argv.
            duration_ms = int((time.monotonic() - start) * 1000)
            return _RunOutcome(
                returncode=-1,
                duration_ms=duration_ms,
                stdout=b"",
                stderr=str(exc).encode("utf-8"),
                stdout_truncated=False,
                stderr_truncated=False,
                timed_out=False,
            )
        duration_ms = int((time.monotonic() - start) * 1000)
        # AIO merges stdout+stderr into one ``output`` stream.
        output = (data.get("output") or "").encode("utf-8")
        stdout, stdout_truncated = cap_stream(output, output_cap)
        return _RunOutcome(
            returncode=int(data.get("exit_code", 0)),
            duration_ms=duration_ms,
            stdout=stdout,
            stderr=b"",
            stdout_truncated=stdout_truncated,
            stderr_truncated=False,
            timed_out=False,
        )

    # -- default transport ------------------------------------------------ #

    def _default_post(
        self, url: str, body: bytes, headers: Mapping[str, str]
    ) -> bytes:
        request = urllib.request.Request(  # noqa: S310 — operator-configured URL
            url, data=body, headers=dict(headers), method="POST"
        )
        with urllib.request.urlopen(  # noqa: S310 — operator-configured endpoint
            request, timeout=self._timeout_s
        ) as resp:
            return resp.read(self._total_cap + 1)
