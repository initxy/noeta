# Sandbox SDK adapters — route fs / shell / browser through `agent-sandbox`

## Goal

Replace noeta's hand-written AIO Sandbox HTTP adapters with implementations
backed by the official `agent-sandbox` Python SDK, behind the **existing**
`ExecEnv` / `BrowserBackend` seams. This adopts the SDK's spec-generated,
authoritative wire contract for file / shell / browser and, as a direct
consequence, fixes the file-read base64 defect.

## Non-goals

- **Container provisioning stays hand-rolled.** `LocalDockerSandboxProvider`
  (`docker run` + resource caps + mounts) is unchanged — the SDK is a pure HTTP
  client and does not provision containers. Only the readiness probe
  (`GET /v1/sandbox`) may optionally move to the SDK.
- **The preview WebSocket proxy stays hand-rolled.** `sandbox_preview_gateway`
  (`/v1/shell/ws` frame pump + noVNC / code-server passthrough) is a streaming
  reverse proxy; the SDK is request/response only and cannot cover it.
- **No model-facing tool-schema change.** Tool `name` / params / description are
  untouched — the stable-prefix KV-cache invariant holds.

## Context

- **Defect being fixed.** `AioSandboxExecEnv.read_bytes` sends
  `{"encoding":"base64"}` to `/v1/file/read` and `base64.b64decode`s the reply.
  The API has **no `encoding` field on read** and returns raw text, so the
  decode corrupts text and raises on binary. Verified failing on both the
  volces image the deployment runs today (`1.0.0.156`) and `1.11.0`. The SDK's
  `file.read_file` takes `{file, start_line, end_line, sudo}` (no encoding) and
  returns `content` directly — correct. `encoding` exists only on `write_file`
  (which noeta already uses correctly).
- **Seams already exist and are injectable.** `ExecEnv` ←
  `_default_backend_factory`; `BrowserBackend` ← `_default_browser_factory`
  (both `packages/noeta-sdk/noeta/client/sandbox.py`, typed `BackendFactory` /
  `BrowserBackendFactory`, already documented as test-injectable). The
  per-session `SandboxExecEnvManager` vends one exec backend + one browser
  backend off each `SandboxHandle` (base_url + auth).
- **SDK facts.** `agent-sandbox` 0.0.30 — a Fern-generated sync (`Sandbox`) /
  async (`AsyncSandbox`) httpx client. Rich `file` module
  (read / write / download / glob / find / grep / list / replace), `shell`
  (`exec_command` with `exec_dir` / timeouts / sessions), and a **semantic**
  browser under `browser_page` (`navigate` / `click` / `fill` / `type` /
  `press_key` / `hover` / `elements` / `get_markdown` / `get_text` / `html` /
  `scroll` / `screenshot`). Deps pull `volcengine-python-sdk` (~5.0.40) +
  pydantic v2.

## Decisions

### D1 — Full cutover, dependency in the app layer *(user-confirmed)*

fs + shell + browser all route through the SDK. New adapters live in
`apps/noeta-agent` (product layer); `noeta-runtime` keeps its minimal
dependency set (httpx only). The adapters are injected through the existing
factory seams. The dependency is the public `agent-sandbox` (PyPI), pinned
exact in `apps/noeta-agent/pyproject.toml` (still 0.0.x — pre-1.0, unstable
wire). Do **not** depend on the internal `bytedance.bytedai` umbrella.

### D2 — Output-shape fidelity is the hard invariant *(non-negotiable)*

The recorded/event-log output is the tool result, not the HTTP wire. Each new
adapter must map the SDK's pydantic results **back to noeta's existing shapes**:
`_RunOutcome{returncode, stdout: bytes, stderr: bytes, stdout_truncated,
stderr_truncated, timed_out, duration_ms}`, raw `bytes` for reads,
`TreeSnapshot`, and the `_AIO_ERROR_TYPES` → `OSError`-subclass mapping
(`not_found`→FileNotFoundError, `permission_denied`→PermissionError,
`already_exists`→FileExistsError). A representative recorded task must diff
clean across the swap.

### D3 — fs ops: native SDK where behavior is preserved *(proposed)*

- `read_text` → `file.read_file` (raw `content`); `read_bytes` for binary →
  `file.download_file`. (This is the fix.)
- `write_bytes` → `file.write_file(encoding="base64")` (unchanged semantics).
- `glob` / `rglob` → `file.glob_files`; `exists` / `is_file` / `is_dir` /
  `is_symlink` → `file.list_path`; `tree_snapshot` → `file.find_files` + batched
  reads, reproducing the current `TreeSnapshot` exactly.
- `unlink` / `mkdir` → native if clean, else keep the `shell.exec_command`
  (`rm` / `mkdir`) form.
- `create_exclusive` → keep the `set -C` noclobber shell dance (no native
  O_EXCL); recovery verbs unchanged.

### D4 — shell: `shell.exec_command` *(proposed)*

`run_argv` → `shell.exec_command(command, exec_dir=cwd, timeout=…)`. Preserve
the `cd`-prefix + per-call preamble, `output_cap`, and the spill-file tail
(`full_output_file_path`) recovery. Map `{status, output, exit_code}` →
`_RunOutcome` (merged stream lands in `stdout`, `stderr` empty — as today).
`supports_background` stays `False`.

### D5 — browser: `browser_page` REST, adapter bridges index↔selector *(user-confirmed; risk noted)*

Model-facing browser tools stay index-based; `SdkBrowserBackend` maps:
`navigate`→`browser_page.navigate`; `get_clickable_elements`→`browser_page.elements`
reformatted to noeta's numbered-list text; `click(index)`→resolve index→selector
from the last elements snapshot then `browser_page.click(selector)`;
`form_input_fill(index)`→`browser_page.fill`; `press_key`→`browser_page.press_key`;
`get_markdown`→`browser_page.get_markdown`; `screenshot`→`browser_page.screenshot`
returned as the image content block noeta expects. The browser is **not currently
broken**, so this phase carries pure regression risk and ships after fs/shell.

### D6 — auth / preamble reuse *(keep)*

The SDK client is built per session from `handle.base_url` +
`handle.auth.connect_headers` (D8 per-call header factory), threaded via
`request_options` / headers so the short-lived credential is minted per request
and never held durably. The preamble stays a per-call command prefix.

## Implementation plan (phased)

1. **Dependency + skeleton.** Add pinned `agent-sandbox` to the app; stub
   `SdkSandboxExecEnv(ExecEnv)` + factory wiring behind config so the old
   adapter stays default until parity is proven.
2. **fs/shell adapter + tests + live verify.** Full `ExecEnv` mapping; fake-SDK
   mapping tests; live parity run against `1.11.0` and `1.0.0.156`. Read/grep
   now succeed — the defect is closed.
3. **browser adapter + index bridge + live verify.** `SdkBrowserBackend`;
   navigate/elements/click/markdown/screenshot parity on a live container.
4. **Cutover.** Flip factory defaults to the SDK adapters; retire
   `AioSandboxExecEnv` / `AioBrowserBackend` (or keep briefly as fallback).

## Acceptance criteria

- `make check` green; new mapping tests pin every `ExecEnv` / `BrowserBackend`
  method's SDK call + result mapping (no live socket).
- Live: driving the SDK adapters against `1.11.0` **and** `1.0.0.156` —
  read/grep succeed; run_argv / write / glob / stat / tree_snapshot at parity;
  browser navigate / elements / click / get_markdown / screenshot at parity.
- A representative recorded task's event-log output shape is unchanged across
  the swap.
- Sandbox image can be pinned to `…/all-in-one-sandbox:1.11.0` in
  `noeta.config.json` (drop the unreproducible `:latest`).

## Risks

- **Browser bridge drift.** index↔selector resolution and the `elements` text
  format are a model-facing behavior change (though not a schema break).
- **Dependency weight / churn.** `volcengine-python-sdk` footprint; 0.0.30 is
  pre-1.0 — pin exact, watch for wire changes.
- **Binary read.** `download_file` path must be exercised (the old code never
  correctly read binary at all).
- **tree_snapshot parity.** Skill indexing depends on the exact `TreeSnapshot`
  output; the native `find_files` mapping must match byte-for-byte.

## Files / areas

- New: `apps/noeta-agent/noeta/agent/host/sdk_sandbox_exec_env.py`,
  `…/sdk_browser_backend.py`.
- Wire: factory injection in `apps/noeta-agent/.../lifecycle.py` / SDK
  `host.py`; factory seams in `packages/noeta-sdk/noeta/client/sandbox.py`
  (already injectable).
- Dep: `apps/noeta-agent/pyproject.toml`.
- Contract reference (do not regress): `packages/noeta-runtime/noeta/tools/fs/exec_env.py`,
  `packages/noeta-runtime/noeta/tools/browser/_backend.py`.

## Implementation notes

### 2026-07-10 — full cutover landed (fs / shell / browser)

Delivered and verified end-to-end against a live `all-in-one-sandbox:1.11.0`
container (and the running `1.0.0.156` build). `make check` green (3226 passed,
`mypy --strict` clean, import-linter 16/0).

- **`SdkSandboxExecEnv`** (`apps/noeta-agent/noeta/agent/host/sdk_sandbox_exec_env.py`)
  subclasses `AioSandboxExecEnv` and overrides ONLY `__init__` / `_shell` /
  `read_bytes` / `write_bytes` → `client.shell.exec_command` /
  `client.file.download_file` / `client.file.write_file`. Every higher-level
  method (glob, stat, tree_snapshot, run_argv, create_exclusive) is inherited
  byte-for-byte. The read defect is fixed: `download_file` streams raw bytes
  (exact for text AND binary) instead of the old `encoding=base64` + `b64decode`.
- **`SdkBrowserBackend`** (`…/host/sdk_browser_backend.py`) implements the
  `BrowserBackend` surface over `browser_page`. **No selector bridge was needed:**
  `browser_page.click` / `fill` accept the numeric `index` natively (each
  `get_elements` row carries its own `index`), so noeta's index-addressed model
  maps straight through.
- **Wiring:** two optional factories on `HostConfig` (`sandbox_backend_factory` /
  `sandbox_browser_factory`), threaded `client.py → SdkHost →
  SandboxExecEnvManager`; `lifecycle.py` injects the SDK factories when
  `sandbox_enabled`. The `ExecEnv` / `BrowserBackend` seam + AIO adapters are now
  re-exported from `noeta.sdk` so the app layer reaches them without breaking the
  `noeta.agent.host` ⇏ `noeta.tools` import ratchet.
- **Dependency:** `agent-sandbox>=0.0.30,<0.1.0` in `apps/noeta-agent` (`<` cap,
  not `==`, to satisfy the lower-bounds-only lint while bounding the 0.0.x wire).
- **Tests:** `tests/test_sdk_sandbox_exec_env.py`, `tests/test_sdk_browser_backend.py`
  (fake-SDK mapping, 21 cases). The screenshot fault-mapping test caught a real
  bug (streaming `b"".join` must run inside the error guard) — fixed.

**Gotcha found (not a regression):** the container `/v1/shell/exec` runs a
persistent shell session, so a command containing a bare `exit N` kills the
session and the call hangs (curl hangs identically). noeta always sends
`cd <cwd> && <real-cmd>`, never a bare session-killer, so it is unaffected — and
this is pre-existing behaviour shared by `AioSandboxExecEnv`.

### 2026-07-10 — review fixes (two-review consolidation)

Correctness fixes to the adapters after cross-review; `make check` green
(3242 passed, protocols mypy clean, import-linter 16/0):

- **`_shell` exit-code passthrough.** A missing `exit_code` (command not
  completed: status running / timeout / terminated) is now passed through as
  ABSENT — the urllib wire's raw-dict semantics — so each inherited consumer
  applies its own default (stat/unlink/mkdir/create_exclusive fail-safe on 1,
  `run_argv` on 0). The previous `None → 0` coercion made the noclobber gate
  and stat read an indeterminate outcome as success.
- **In-band failures surfaced.** `_shell` / `write_bytes` now check
  `response.success is False` (the v1 wire's 200-with-`success:false` channel,
  which the Fern client parses without raising) and map `data.error_type`
  through the same `not_found`/`permission_denied`/`already_exists` → OSError
  table the urllib adapter used.
- **`read_bytes` bounded.** The `download_file` reassembly enforces the same
  32MB `total_cap` the urllib backend put on a response body.
- **Page screenshot.** `browser.screenshot` captures the container DISPLAY;
  switched to `browser_page.screenshot` (the page — the old MCP
  `browser_screenshot` semantics).
- **Seam typing.** `BrowserBackendFactory` / the manager's browser cache /
  `resolve_browser` are typed against the `BrowserBackend` Protocol (not the
  concrete AIO class); `BackendFactory` / `BrowserBackendFactory` /
  `BoundPreamble` re-exported from `noeta.sdk`; the lifecycle factories are
  annotated.
- **Base-class tripwire.** `SdkSandboxExecEnv` overrides `_call` to raise a
  self-describing error if a future `AioSandboxExecEnv` method bypasses
  `_shell`/`read_bytes`/`write_bytes` (the subclass never builds the urllib
  transport); dead `_timeout_s` attribute removed, `preamble` type aligned.
- **`close()` lifecycle.** Both adapters expose idempotent `close()` for their
  owned httpx pool; `SandboxExecEnvManager` best-effort duck-typed-closes
  evicted backends on `release` / `teardown` (`close` is NOT part of the seam).
- **Tests**: the vacuous `is False or True` assertion replaced with real
  semantics pins; added spill-tail recovery, the pydantic `extra="allow"`
  spill-field pin, timeout→timed-out-run mapping, success-false (shell+write),
  read-cap, create_exclusive (incl. the indeterminate gate), tree_snapshot
  listing, preamble concatenation, and manager close-on-eviction coverage.

### 2026-07-10 — open items decided (maintainer sign-off)

- **No config kill-switch.** SDK adapters are injected unconditionally when the
  sandbox is enabled; rollback is a two-line code revert at the injection site
  (the factory seam is the switch), and the old wire's file-read is defective
  anyway. Decision recorded in the execution-environment-seam ADR.
- **Export surface narrowed.** `noeta.sdk` exposes only the seam protocols +
  factory types (`ExecEnv` / `BrowserBackend` / `BackendFactory` /
  `BrowserBackendFactory` / `BoundPreamble`); the concrete AIO adapters left
  the public surface, and the two `noeta.agent.host.sdk_*` modules reach them
  via pinned import-linter exemptions in `app-uses-only-sdk`. ADR §"SDK-adapter
  export surface"; CONTEXT.md exemption list updated.
- **Live checks scripted, pending a container.**
  `scripts/verify-sandbox-sdk-live.py` (needs `NOETA_TEST_AIO_SANDBOX_URL` or a
  URL argument) probes: whether `browser_page.fill` clears existing field
  content (the old wire sent `clear: true`), and whether the image signals
  file faults via HTTP status or `200 + success:false` (both channels are
  handled in the adapter; this confirms which one the deployed images
  — `1.11.0` / `1.0.0.156` — actually exercise).

### Not done (deliberate, deployment-owner's call)

Pinning `noeta.config.json`'s `sandbox_image` to `…:1.11.0` (dropping `:latest`)
is left to the operator — it changes a live deployment's image.
