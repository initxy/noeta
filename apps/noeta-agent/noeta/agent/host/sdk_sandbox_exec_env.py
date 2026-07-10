"""``SdkSandboxExecEnv`` — the AIO Sandbox fs/shell backend over the official SDK.

The product-layer replacement for the hand-written HTTP wire in
:class:`~noeta.tools.fs.exec_env.AioSandboxExecEnv`: it routes every file / shell
side effect through the official ``agent-sandbox`` client (``client.shell`` /
``client.file``) instead of a hand-rolled ``urllib`` POST.

**It is deliberately a thin subclass.** In ``AioSandboxExecEnv`` all of
``glob`` / ``rglob`` / ``exists`` / ``is_file`` / ``mkdir`` / ``unlink`` /
``create_exclusive`` / ``tree_snapshot`` / ``run_argv`` are expressed on top of
just three transport primitives — ``_shell`` (a ``/v1/shell/exec`` call),
``read_bytes`` and ``write_bytes``. Overriding ONLY those three to call the SDK
leaves every higher-level method (and its exact recorded output shape) inherited
byte-for-byte, so the event-log/tool contract is unchanged while the transport
becomes the SDK. It also fixes the read defect: the old ``read_bytes`` sent a
non-existent ``encoding=base64`` field to ``/v1/file/read`` and ``b64decode``d a
raw-text reply; here ``read_bytes`` uses ``file.download_file`` (raw bytes, exact
for text AND binary).

Errors are re-mapped to the same ``OSError`` subclasses the tools branch on
(``FileNotFoundError`` / ``PermissionError`` / :class:`AioSandboxError`), so tool
code stays backend-agnostic. The SDK raises :class:`ApiError` on non-2xx; the
status code carries the fault class.

Lives in the product layer (``noeta.agent.host``) because the SDK dependency
(``agent-sandbox`` + its transitive ``volcengine-python-sdk``) is a product
concern — ``noeta-runtime`` keeps its minimal footprint. Injected through the
``SandboxExecEnvManager`` backend factory. See
``docs/implementation-specs/2026-07-10-sandbox-sdk-adapters.md``.
"""

from __future__ import annotations

import base64
from collections.abc import Sequence
from pathlib import Path
from typing import Any, Callable, Mapping, Optional

import httpx
from agent_sandbox import Sandbox
from agent_sandbox.core.api_error import ApiError

# The concrete AIO adapter is runtime-internal (NOT on the ``noeta.sdk`` public
# surface — it is slated for retirement and must not become user-facing API).
# This module is one of the two pinned import-linter exemptions that may reach
# it directly (see the execution-environment-seam ADR, "SDK-adapter export
# surface", and the ``app-uses-only-sdk`` contract).
from noeta.tools.fs.exec_env import (
    DEFAULT_AIO_TIMEOUT_S,
    AioSandboxError,
    AioSandboxExecEnv,
)


__all__ = ["SdkSandboxExecEnv"]

#: Cap on one read's reassembled bytes — the same bound the urllib backend puts
#: on a response body (``_DEFAULT_AIO_TOTAL_CAP`` in ``noeta.tools.fs.exec_env``,
#: not importable here: the app layer reaches runtime only through ``noeta.sdk``).
_DEFAULT_TOTAL_CAP = 32 * 1024 * 1024

#: In-band ``data.error_type`` → the stdlib ``OSError`` subclass the tools
#: branch on. Mirrors ``_AIO_ERROR_TYPES`` in ``noeta.tools.fs.exec_env`` (same
#: import constraint as the cap above).
_ERROR_TYPES: dict[str, type[OSError]] = {
    "not_found": FileNotFoundError,
    "permission_denied": PermissionError,
    "already_exists": FileExistsError,
}


class SdkSandboxExecEnv(AioSandboxExecEnv):
    """:class:`~noeta.tools.fs.exec_env.ExecEnv` backed by the ``agent-sandbox`` SDK.

    Overrides the three transport primitives (``_shell`` / ``read_bytes`` /
    ``write_bytes``); everything else is inherited from
    :class:`AioSandboxExecEnv` unchanged.
    """

    def __init__(
        self,
        *,
        base_url: str,
        api_key: Optional[str] = None,
        auth_headers: Optional[Callable[[], Mapping[str, str]]] = None,
        preamble: Optional[Callable[[Sequence[str]], str]] = None,
        timeout_s: float = DEFAULT_AIO_TIMEOUT_S,
        total_cap: int = _DEFAULT_TOTAL_CAP,
        client: Optional[Sandbox] = None,
    ) -> None:
        if not base_url:
            raise AioSandboxError("aio sandbox base_url is empty")
        # Only the attributes the inherited methods actually read: ``_preamble``
        # (run_argv) and ``_auth_headers`` (folded into per-call request options
        # below). We intentionally do NOT call ``super().__init__`` — it builds
        # the urllib transport this subclass replaces; the ``_call`` override
        # below turns any future bypass of the three primitives into a loud,
        # self-describing failure instead of an ``AttributeError``.
        self._preamble = preamble
        self._auth_headers = auth_headers
        self._total_cap = total_cap
        static_headers: dict[str, str] = {}
        if api_key:
            # v1 static-key path: fold into constant headers. Per-call auth (D8)
            # rides via ``request_options`` instead (see ``_request_options``).
            static_headers["X-AIO-API-Key"] = api_key
        # ``trust_env=False``: the container is addressed at 127.0.0.1; a loopback
        # call must never be routed through an ambient HTTP(S)_PROXY (that hangs).
        # Mirrors the urllib backend, which does not proxy loopback either. An
        # explicit ``client`` is injected by tests (a fake exposing ``.shell`` /
        # ``.file``); ``_httpx_client`` is kept only when this instance built (and
        # therefore owns) the pool, so ``close`` never touches an injected one.
        self._httpx_client: Optional[httpx.Client] = None
        if client is None:
            self._httpx_client = httpx.Client(timeout=timeout_s, trust_env=False)
            client = Sandbox(
                base_url=base_url.rstrip("/"),
                headers=static_headers or None,
                httpx_client=self._httpx_client,
            )
        self._client: Sandbox = client

    def close(self) -> None:
        """Release the owned HTTP connection pool (idempotent, never raises).

        Called by ``SandboxExecEnvManager`` when a session's backend is evicted
        (release / teardown); a no-op for an injected test client.
        """
        if self._httpx_client is not None:
            try:
                self._httpx_client.close()
            except Exception:  # noqa: BLE001 — teardown path must not raise
                pass

    # -- per-call request options (timeout + D8 per-call auth) ------------- #

    def _request_options(self, timeout_s: Optional[float]) -> Optional[dict[str, Any]]:
        opts: dict[str, Any] = {}
        if timeout_s is not None:
            opts["timeout_in_seconds"] = int(timeout_s)
        if self._auth_headers is not None:
            headers = self._auth_headers()
            if headers:
                opts["additional_headers"] = dict(headers)
        return opts or None

    # -- error mapping ---------------------------------------------------- #

    @staticmethod
    def _api_error_message(exc: ApiError) -> str:
        body = exc.body
        if isinstance(body, Mapping):
            message = body.get("message")
            if isinstance(message, str) and message:
                return message
        if isinstance(body, str) and body:
            return body
        return f"request failed (status {exc.status_code})"

    def _file_error(self, exc: ApiError, path: Path) -> OSError:
        """Map an SDK file :class:`ApiError` to the OSError subclass tools expect.

        The status code carries the fault class; ``404`` MUST become
        ``FileNotFoundError`` (the read tool and instruction restore branch on
        it). Everything else degrades to :class:`AioSandboxError`.
        """
        status = exc.status_code
        message = self._api_error_message(exc)
        if status == 404:
            return FileNotFoundError(f"{path}: {message}")
        if status in (401, 403):
            return PermissionError(f"{path}: {message}")
        return AioSandboxError(f"{path}: {message}")

    @staticmethod
    def _response_failure(response: Any, context: str) -> OSError:
        """Map a 2xx ``success: false`` reply to the OSError subclass tools expect.

        The v1 wire signals most faults in-band (HTTP 200 + ``success: false`` +
        ``data.error_type``), not via HTTP status — the Fern client parses such a
        reply without raising, so the adapter must check. Mirrors the urllib
        adapter's ``_error``: ``error_type`` picks the stdlib subclass
        (``not_found`` → FileNotFoundError, …), anything else degrades to
        :class:`AioSandboxError`. ``error_type`` is not a declared SDK model
        field; ``getattr`` reads it off the ``extra="allow"`` pydantic model.
        """
        message = getattr(response, "message", None) or f"{context}: request failed"
        error_type = getattr(getattr(response, "data", None), "error_type", None)
        cls = _ERROR_TYPES.get(error_type or "", AioSandboxError)
        return cls(message)

    def _call(
        self, path: str, body: dict[str, Any], *, timeout_s: Optional[float] = None
    ) -> dict[str, Any]:
        """Tripwire: this subclass has no urllib wire.

        Every parent method reaches its transport through ``_shell`` /
        ``read_bytes`` / ``write_bytes`` (all overridden). If a future
        ``AioSandboxExecEnv`` change calls ``_call`` directly, fail with a
        diagnosis instead of an ``AttributeError`` on the parent attributes this
        ``__init__`` deliberately never sets.
        """
        raise AioSandboxError(
            f"SdkSandboxExecEnv has no urllib wire for {path}: a new "
            "AioSandboxExecEnv method bypassed _shell/read_bytes/write_bytes"
        )

    # -- transport primitives (the ONLY overrides) ------------------------ #

    def _shell(
        self, command: str, *, timeout_s: Optional[float] = None
    ) -> dict[str, Any]:
        """Run ``command`` via ``client.shell.exec_command`` and return the
        ``{output, exit_code, full_output_file_path}`` dict the inherited methods
        parse (the same shape the old ``/v1/shell/exec`` ``data`` object had)."""
        try:
            response = self._client.shell.exec_command(
                command=command, request_options=self._request_options(timeout_s)
            )
        except httpx.TimeoutException as exc:
            # Surface as builtin TimeoutError so ``run_argv`` classifies a wedged
            # command as a timed-out run (and every other caller sees an OSError).
            raise TimeoutError(f"/v1/shell/exec timed out: {exc}") from exc
        except ApiError as exc:
            raise AioSandboxError(
                f"/v1/shell/exec: {self._api_error_message(exc)}"
            ) from exc
        except AioSandboxError:
            raise
        except Exception as exc:  # any other transport fault
            raise AioSandboxError(f"/v1/shell/exec: transport error: {exc}") from exc
        if response.success is False:
            # The v1 wire's in-band failure channel (200 + success:false); the
            # Fern client parses it without raising, so surface it here.
            raise self._response_failure(response, "/v1/shell/exec")
        data = response.data
        if data is None:
            raise AioSandboxError("/v1/shell/exec: response missing data")
        result: dict[str, Any] = {"output": data.output}
        # Pass ``exit_code`` through only when the server reported one. A missing
        # code means the command did NOT complete (status running / timeout /
        # terminated), and each inherited consumer already applies the right
        # default for that — the stat / unlink / mkdir / create_exclusive
        # failure branches assume 1, ``run_argv`` assumes 0 — the same
        # passthrough semantics the urllib wire's raw ``data`` dict had.
        if data.exit_code is not None:
            result["exit_code"] = data.exit_code
        spill = getattr(data, "full_output_file_path", None)
        if isinstance(spill, str) and spill:
            result["full_output_file_path"] = spill
        return result

    def read_bytes(self, path: Path) -> bytes:
        """Read raw file bytes via ``client.file.download_file``.

        ``download_file`` streams the exact bytes (correct for text AND binary),
        so this is the byte-exact path the old ``read_bytes`` never had. The
        reassembly is bounded by ``total_cap`` — the same bound the urllib
        backend put on a response body, so a huge container file raises a clean
        error instead of exhausting host memory.
        """
        try:
            chunks = self._client.file.download_file(
                path=str(path), request_options=self._request_options(None)
            )
            buf = bytearray()
            for chunk in chunks:
                buf.extend(chunk)
                if len(buf) > self._total_cap:
                    raise AioSandboxError(f"read {path}: response exceeded total cap")
            return bytes(buf)
        except ApiError as exc:
            raise self._file_error(exc, path) from exc
        except AioSandboxError:
            raise
        except httpx.TimeoutException as exc:
            raise TimeoutError(f"read {path} timed out: {exc}") from exc
        except Exception as exc:
            raise AioSandboxError(f"read {path}: transport error: {exc}") from exc

    def write_bytes(self, path: Path, body: bytes) -> None:
        try:
            response = self._client.file.write_file(
                file=str(path),
                content=base64.b64encode(body).decode("ascii"),
                encoding="base64",
                request_options=self._request_options(None),
            )
        except ApiError as exc:
            raise self._file_error(exc, path) from exc
        except AioSandboxError:
            raise
        except httpx.TimeoutException as exc:
            raise TimeoutError(f"write {path} timed out: {exc}") from exc
        except Exception as exc:
            raise AioSandboxError(f"write {path}: transport error: {exc}") from exc
        if response.success is False:
            # In-band failure (200 + success:false + data.error_type) — the v1
            # wire's primary channel for write faults; a silent drop here would
            # let edit / apply_patch report success with the file unchanged.
            raise self._response_failure(response, f"write {path}")
