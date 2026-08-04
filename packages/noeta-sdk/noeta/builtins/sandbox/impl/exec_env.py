"""``AioSandboxExecEnv`` — the ``ExecEnv`` implementation backed by an AIO
Sandbox container over HTTP.

The entire container wire contract is isolated in this one module, so a
container API drift is a one-file change and the tool code above the ``ExecEnv``
seam never learns which executor it runs on.
"""

from __future__ import annotations

import base64
import codecs
import json
import shlex
import time
import urllib.error
import urllib.request
from collections.abc import Iterable, Sequence
from pathlib import Path
from typing import Any, Callable, Mapping, Optional, Protocol

from noeta.runtime.exec_env import (
    ExclusiveCreateExists,
    ExclusiveCreateWriteFailed,
    TreeSnapshot,
    SubprocRunner,
)
from noeta.runtime.subproc import RunOutcome, cap_stream


__all__ = [
    "AioHttpPost",
    "AioSandboxError",
    "AioSandboxExecEnv",
    "DEFAULT_AIO_TIMEOUT_S",
]


#: Default per-call HTTP timeout for a sandbox request (seconds). ``run_argv``
#: overrides it per call from the tool's own timeout budget.
DEFAULT_AIO_TIMEOUT_S = 60.0

#: Cap on a single response body (bytes) — bounds memory the same way the MCP
#: HTTP client does.
_DEFAULT_AIO_TOTAL_CAP = 32 * 1024 * 1024

class AioHttpPost(Protocol):
    """Injectable HTTP transport: ``(url, json_body, headers, *, timeout_s)`` →
    raw response body bytes.

    Injectable so tests substitute a fake and never open a socket; leaving it
    ``None`` uses stdlib ``urllib``. ``timeout_s`` is the already-resolved socket
    read timeout for THIS call — the adapter default for file/stat ops, or the
    caller's per-command budget for a ``run_argv`` shell exec. The container
    never hard-kills a slow exec, so this transport timeout IS the effective
    bound.
    """

    def __call__(
        self, url: str, body: bytes, headers: Mapping[str, str], *, timeout_s: float
    ) -> bytes: ...


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

#: Guard exit codes for the shell-side ``read_bytes`` path: the guards run
#: before ``base64`` so a stat fault refines into the same stdlib ``OSError``
#: subclass the ``error_type`` map gives the ``/v1/file/*`` endpoints
#: (``base64``'s own exit code cannot tell the two apart).
_READ_EXIT_NOT_FOUND = 40
_READ_EXIT_NO_PERM = 41


class AioSandboxExecEnv:
    """:class:`ExecEnv` backed by an AIO Sandbox container over HTTP.

    Every file and process side effect is routed to one ``agent-infra/sandbox``
    container — ``POST /v1/shell/exec`` for process execution, ``POST
    /v1/file/{read,write}`` for file IO — instead of the local host. The tool
    code above the seam is identical either way, so a tool's model-facing
    contract and its recorded output shape do not depend on the executor.

    The wire contract (field names, the base64 write encoding, the shell-side
    ``base64`` read path, the merged ``output`` stream) is captured only here
    and pinned by fake-transport tests. The mapping decisions worth calling out:

    * **``run_argv``** joins the argv with :func:`shlex.join` and prefixes
      ``cd <cwd> && `` — cwd is expressed lexically rather than relying on an
      unconfirmed request field. An optional host-supplied ``preamble``, minted
      fresh each call, is inserted between the ``cd`` and the command so a
      session can establish per-exec shell state. The container returns a single
      merged ``output`` stream, so it lands in ``stdout`` and ``stderr`` stays
      empty; nothing downstream requires the split. A large run's inline
      ``output`` is truncated with the full stream spilled to a container file
      named by ``full_output_file_path``, and ``run_argv`` reads that file's
      tail (see ``_read_spill``) so the recovered bytes — not the lossy inline
      echo — feed the ``output_cap`` and a big build log never trips the
      response ``_total_cap``.
    * **byte fidelity** — ``/v1/file/read`` is TEXT-ONLY: ``FileReadRequest``
      carries no ``encoding`` field (confirmed against the v1.11.0 OpenAPI;
      read is asymmetric with write, which does take ``encoding="base64"``)
      and ``content`` arrives as server-decoded UTF-8 text. ``read_text`` uses
      the endpoint natively; the byte-exact path (``read_bytes`` — edit hashes
      the bytes for TOCTOU, image reads need them verbatim) instead runs
      ``base64 -w0`` through ``/v1/shell/exec`` and decodes locally, with
      guard exit codes refining missing/unreadable into ``FileNotFoundError``
      / ``PermissionError``. Writes send base64 to ``/v1/file/write``.
    * **``glob`` / ``rglob``** are expressed with shell ``globstar`` (``rglob``
      = ``glob('**/'+pattern)``, matching pathlib's own definition), so they
      depend on ``bash`` with ``globstar`` in the image. Their pathlib semantics
      are a best-effort approximation validated only against a live container
      (gated ``NOETA_TEST_AIO_SANDBOX_URL``).
    * **stat** (``exists`` / ``is_file`` / …) runs ``test`` and reads the exit
      code; **``unlink``** runs ``rm``.
    * **``tree_snapshot``** folds a whole recursive walk plus selected reads
      into ONE ``find``-driven exec — bulk consumers such as skill indexing use
      it instead of a per-file ``rglob`` / ``is_file`` / ``read_text`` loop,
      whose per-round-trip fixed cost dominates at 100+ files.

    ``fence_token`` is reserved: it is always ``None`` and no fence header is
    sent.
    """

    def __init__(
        self,
        *,
        base_url: str,
        api_key: Optional[str] = None,
        auth_headers: Optional[Callable[[], Mapping[str, str]]] = None,
        preamble: Optional[Callable[[Sequence[str]], str]] = None,
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
        self._fence_token = fence_token
        # ``auth_headers`` is a per-call header factory, so a short-lived
        # credential is minted fresh for EACH request rather than fixed at
        # construction. Supplying it bypasses the static ``api_key`` path.
        # Either way auth rides only on the wire — never recorded.
        self._auth_headers = auth_headers
        # ``preamble`` is the process twin of ``auth_headers``: a host callable
        # invoked fresh for each ``run_argv`` and prepended verbatim ahead of the
        # command, so a session can establish per-exec shell state (say a
        # credential that expires mid-session). It receives the argv, so the host
        # can tailor the prefix to the command, and must return a complete prefix
        # INCLUDING its own separator (``export X=Y && `` / ``foo; ``); ``""`` is
        # a no-op. It is a host runtime injection, never LLM-controlled and never
        # recorded, and it must be total — a raise propagates into the run.
        self._preamble = preamble
        headers = {"Content-Type": "application/json"}
        if api_key:
            # The container accepts the key via X-AIO-API-Key, Authorization:
            # Bearer, or ?api_key=. The header form keeps it off the URL, and it
            # rides only on the wire — never recorded.
            headers["X-AIO-API-Key"] = api_key
        self._headers = headers

    def _request_headers(self) -> Mapping[str, str]:
        """The headers for one request — the static dict, or freshly minted when
        an ``auth_headers`` factory is wired.
        """
        if self._auth_headers is None:
            return self._headers
        return {"Content-Type": "application/json", **self._auth_headers()}

    # -- wire ------------------------------------------------------------- #

    def _call(
        self, path: str, body: dict[str, Any], *, timeout_s: Optional[float] = None
    ) -> dict[str, Any]:
        """POST ``body`` to ``path`` and return the response ``data`` object.

        Raises the mapped :class:`OSError` subclass on ``success=false`` and
        :class:`AioSandboxError` on any transport / protocol fault. ``timeout_s``
        is the socket read timeout for this one call; ``None`` uses the
        adapter-level default (file/stat ops), while ``run_argv`` passes the
        caller's per-command budget so a long ``shell_run`` is bounded by the
        timeout the model asked for. The container does not hard-kill a slow
        ``exec``, so this client timeout IS the effective bound — when it fires
        the command may still be running there until its lease cap.
        """
        url = self._base + path
        data = json.dumps(body, separators=(",", ":")).encode("utf-8")
        effective_timeout = self._timeout_s if timeout_s is None else timeout_s
        try:
            raw = self._post(
                url, data, self._request_headers(), timeout_s=effective_timeout
            )
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

    def _shell(
        self, command: str, *, timeout_s: Optional[float] = None
    ) -> dict[str, Any]:
        return self._call(
            "/v1/shell/exec", {"command": command}, timeout_s=timeout_s
        )

    # -- file reads ------------------------------------------------------- #

    def read_bytes(self, path: Path) -> bytes:
        # ``/v1/file/read`` cannot return bytes (see the class docstring), so
        # the byte-exact path encodes in the container and decodes here — the
        # same transport ``tree_snapshot`` already leans on. ``validate=True``
        # makes any non-base64 leakage (an error message in the merged output
        # stream) a loud fault instead of silently corrupt bytes. The guards
        # run in a subshell because the exec session's shell is persistent — a
        # bare ``exit`` kills it and wedges the request (verified live).
        q = shlex.quote(str(path))
        outcome = self._shell(
            f"( if [ ! -e {q} ]; then exit {_READ_EXIT_NOT_FOUND}; "
            f"elif [ ! -r {q} ]; then exit {_READ_EXIT_NO_PERM}; "
            f"else base64 -w0 -- {q}; fi )"
        )
        code = int(outcome.get("exit_code", 1))
        if code == _READ_EXIT_NOT_FOUND:
            raise FileNotFoundError(f"read {path}: no such file")
        if code == _READ_EXIT_NO_PERM:
            raise PermissionError(f"read {path}: permission denied")
        if code != 0:
            raise AioSandboxError(f"read {path}: {outcome.get('output', '')!r}")
        text = outcome.get("output") or ""
        spill_path = outcome.get("full_output_file_path")
        if isinstance(spill_path, str) and spill_path:
            # Inline echo truncated, full stream spilled to a container file.
            # The spill is pure ASCII base64, so the text endpoint reads it
            # whole with no fidelity concern (bounded by ``_total_cap`` like
            # any response).
            text = self._read_content(spill_path)
        try:
            return base64.b64decode("".join(text.split()), validate=True)
        except (ValueError, TypeError) as exc:
            raise AioSandboxError(f"read {path}: bad base64 content: {exc}") from exc

    def read_text(self, path: Path, *, encoding: str = "utf-8") -> str:
        if codecs.lookup(encoding).name != "utf-8":
            # The endpoint decodes as UTF-8 server-side; any other encoding
            # must take the byte-exact path and decode locally.
            return self.read_bytes(path).decode(encoding)
        return self._read_content(str(path))

    def _read_content(self, path: str) -> str:
        """Whole-file text via ``/v1/file/read`` — the endpoint's native shape."""
        data = self._call("/v1/file/read", {"file": path})
        content = data.get("content")
        if not isinstance(content, str):
            raise AioSandboxError(f"read {path}: response missing 'content'")
        return content

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
        # The container API has no O_EXCL; emulate exclusivity with a noclobber
        # gate so a concurrent create loses the race deterministically. ``set -C``
        # makes ``>`` fail if the target exists, and only once that gate succeeds
        # do we write the real body. The two failure verbs differ on purpose: a
        # pre-existing path never became ours, while a gate that opened but whose
        # body write failed leaves a file the caller must delete.
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
        # ``mkdir -p`` = parents=True, exist_ok=True — the same semantics
        # ``LocalExecEnv.mkdir`` gives on the host.
        outcome = self._shell(f"mkdir -p -- {shlex.quote(str(path))}")
        if int(outcome.get("exit_code", 1)) != 0:
            raise AioSandboxError(f"mkdir {path}: {outcome.get('output', '')!r}")

    @property
    def supports_background(self) -> bool:
        # There is no container-side durable job handle, and the host runner
        # would spawn on the HOST rather than in the container — so ``shell_run``
        # refuses a background launch cleanly instead of running it elsewhere.
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

    def mtime(self, path: Path) -> float:
        # One container stat per path is a round-trip too many for a walk;
        # ``Glob`` degrades to its alphabetical tiebreak in sandbox mode.
        return 0.0

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

    def tree_snapshot(
        self, roots: Sequence[Path], *, content_name: str
    ) -> TreeSnapshot:
        # ONE shell exec for the whole walk: ``find -L`` lists every regular file
        # under the roots (symlinks followed, matching the host indexer walk; a
        # missing root is silenced), and the loop emits one self-describing line
        # per file:
        #
        #   ``F <b64(path)>``                    — a listed file
        #   ``C <b64(path)> <b64(bytes)>``       — a listed ``content_name``
        #                                          file with its bytes inlined
        #
        # Both fields are base64 (no spaces or newlines), so paths with any
        # shell-hostile characters parse unambiguously; an unreadable content
        # file degrades to an empty inline body (listed, no bytes). stderr is
        # dropped because the container merges it into the one ``output`` stream
        # we are about to parse.
        if not roots:
            return TreeSnapshot((), {})
        q_roots = " ".join(shlex.quote(str(r)) for r in roots)
        q_name = shlex.quote(content_name)
        command = (
            f"find -L {q_roots} -type f 2>/dev/null | while IFS= read -r f; do "
            "p=$(printf '%s' \"$f\" | base64 -w0); "
            f'if [ "${{f##*/}}" = {q_name} ]; then '
            "c=$(base64 -w0 < \"$f\" 2>/dev/null) || c=; "
            "printf 'C %s %s\\n' \"$p\" \"$c\"; "
            "else printf 'F %s\\n' \"$p\"; fi; done"
        )
        data = self._shell(command)
        if int(data.get("exit_code", 1)) != 0:
            raise AioSandboxError(
                f"tree_snapshot: walk failed: {data.get('output', '')!r}"
            )
        text = data.get("output") or ""
        spill_path = data.get("full_output_file_path")
        if isinstance(spill_path, str) and spill_path:
            # The inline echo was truncated and the full stream spilled to a
            # container file — parse the spill, never the lossy echo. Unlike
            # run_argv's tail, the snapshot needs the WHOLE stream; the listing
            # is ASCII (both fields base64), so the text endpoint reads it
            # directly, and a skill-tier listing is orders of magnitude below
            # the response cap.
            text = self._read_content(spill_path)
        files: set[Path] = set()
        contents: dict[Path, bytes] = {}
        for line in text.splitlines():
            if not line:
                continue
            kind, _, rest = line.partition(" ")
            try:
                if kind == "F" and rest:
                    path = Path(base64.b64decode(rest, validate=True).decode("utf-8"))
                elif kind == "C" and rest:
                    b64_path, _, b64_body = rest.partition(" ")
                    path = Path(
                        base64.b64decode(b64_path, validate=True).decode("utf-8")
                    )
                    contents[path] = base64.b64decode(b64_body, validate=True)
                else:
                    raise ValueError("unknown line shape")
            except (ValueError, UnicodeDecodeError) as exc:
                raise AioSandboxError(
                    f"tree_snapshot: malformed listing line {line!r}: {exc}"
                ) from exc
            files.add(path)
        return TreeSnapshot(tuple(sorted(files)), contents)

    # -- process ---------------------------------------------------------- #

    def run_argv(
        self,
        argv: list[str],
        *,
        cwd: Path,
        timeout_s: int,
        output_cap: int,
        runner: Optional[SubprocRunner] = None,
    ) -> RunOutcome:
        # ``runner`` is the local subprocess seam — irrelevant remotely, ignored.
        # cwd is expressed lexically (cd &&) rather than via an unconfirmed
        # request field; argv is shell-quoted so the remote shell re-runs the
        # exact tokens.
        del runner
        preamble = self._preamble(argv) if self._preamble is not None else ""
        command = f"cd {shlex.quote(str(cwd))} && {preamble}{shlex.join(argv)}"
        # No remote hard-kill, so the transport read timeout IS the bound: thread
        # the caller's per-command budget through so ``timeout=`` behaves like the
        # local backend's ``subprocess`` timeout. A wedged command keeps running
        # in the container until its lease cap; we still report it as a timed-out
        # run below.
        start = time.monotonic()
        try:
            data = self._shell(command, timeout_s=timeout_s)
        except TimeoutError as exc:
            # socket.timeout is TimeoutError on 3.10+; a URLError-wrapped
            # timeout instead surfaces as AioSandboxError below (still a
            # reported failed run, not a crash).
            duration_ms = int((time.monotonic() - start) * 1000)
            return RunOutcome(
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
            return RunOutcome(
                returncode=-1,
                duration_ms=duration_ms,
                stdout=b"",
                stderr=str(exc).encode("utf-8"),
                stdout_truncated=False,
                stderr_truncated=False,
                timed_out=False,
            )
        duration_ms = int((time.monotonic() - start) * 1000)
        # The container merges stdout+stderr into one ``output`` stream, so
        # ``stderr`` stays empty. When that stream is large the inline ``output``
        # is truncated and the FULL stream spilled to a container file named by
        # ``full_output_file_path``; read its tail so a big build log lands in
        # the artifact instead of being lost to truncation — or, worse, exceeding
        # the response ``_total_cap`` and failing the whole run. A spill read that
        # faults degrades to the truncated inline output, and an image that never
        # sends the field takes the same path.
        inline = (data.get("output") or "").encode("utf-8")
        spill_path = data.get("full_output_file_path")
        if isinstance(spill_path, str) and spill_path:
            recovered = self._read_spill(spill_path, output_cap)
            output = recovered if recovered else inline
        else:
            output = inline
        stdout, stdout_truncated = cap_stream(output, output_cap)
        return RunOutcome(
            returncode=int(data.get("exit_code", 0)),
            duration_ms=duration_ms,
            stdout=stdout,
            stderr=b"",
            stdout_truncated=stdout_truncated,
            stderr_truncated=False,
            timed_out=False,
        )

    def _read_spill(self, path: str, cap: int) -> bytes:
        """Return the bounded tail of a ``full_output_file_path`` spill.

        Reading the whole file would hit the very response ``_total_cap`` the
        spill exists to dodge, so pull only the last
        ``cap + 1`` bytes with ``tail -c`` (the ``+1`` lets ``cap_stream`` still
        flag truncation). ``tail``'s own output is bounded by ``cap`` and so is
        always well under the response cap. Any read fault (missing file,
        transport error, non-zero ``tail`` exit) returns empty so the caller
        falls back to the truncated inline output rather than crashing the run.
        """
        try:
            outcome = self._shell(f"tail -c {cap + 1} -- {shlex.quote(path)}")
        except OSError:
            return b""
        if int(outcome.get("exit_code", 1)) != 0:
            return b""
        return (outcome.get("output") or "").encode("utf-8")

    # -- default transport ------------------------------------------------ #

    def _default_post(
        self, url: str, body: bytes, headers: Mapping[str, str], *, timeout_s: float
    ) -> bytes:
        request = urllib.request.Request(  # noqa: S310 — operator-configured URL
            url, data=body, headers=dict(headers), method="POST"
        )
        with urllib.request.urlopen(  # noqa: S310 — operator-configured endpoint
            request, timeout=timeout_s
        ) as resp:
            return resp.read(self._total_cap + 1)
