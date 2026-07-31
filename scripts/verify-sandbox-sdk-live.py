#!/usr/bin/env python3
"""Probe a live AIO Sandbox container through the ``agent-sandbox`` client.

The ``sandbox`` built-in reaches a container over its own hand-written wire —
``AioSandboxExecEnv`` on ``/v1/file/*`` and ``/v1/shell/exec``,
``AioBrowserBackend`` on ``/mcp`` — and both adapters lean on container
behaviour that the generated ``agent-sandbox`` client spells differently. A
host swapping in client-backed backends needs those two answers from a real
image; neither is knowable from the fake transports the contract tests use,
hence a script rather than a test.

Probe 1 asks whether ``browser_page.fill`` replaces a field's value or appends
to it. ``AioBrowserBackend.type`` buys replacement by sending ``clear: true``
on ``browser_form_input_fill``, and the client's ``fill`` takes no such
argument — so on "appends", a client-backed backend has to clear the field
itself.

Probe 2 asks which channel a file fault arrives on. ``AioSandboxExecEnv``
refines in-band ``200 + success:false`` (keyed by ``data.error_type``) into the
stdlib ``OSError`` subclass a local backend would raise, whereas the client
raises only on a non-2xx status. A backend reading the wrong channel reports a
missing file as a success.

Requires ``agent-sandbox`` importable. Nothing on the host is touched, and the
only container write is one attempt at a path that cannot exist.

Usage::

    NOETA_TEST_AIO_SANDBOX_URL=http://127.0.0.1:8080 \
        uv run python scripts/verify-sandbox-sdk-live.py
    # or: uv run python scripts/verify-sandbox-sdk-live.py http://127.0.0.1:8080
"""

from __future__ import annotations

import os
import sys

import httpx
from agent_sandbox import Sandbox
from agent_sandbox.core.api_error import ApiError

#: The input mirrors its live value into the DOM attribute, so an image whose
#: ``evaluate`` is unavailable can still be read at the attribute level.
_FILL_PAGE = (
    "data:text/html,<input id='probe' "
    "oninput=\"this.setAttribute('value', this.value)\">"
)


def _client(base_url: str) -> Sandbox:
    return Sandbox(
        base_url=base_url.rstrip("/"),
        httpx_client=httpx.Client(timeout=60.0, trust_env=False),
    )


def probe_fill_clear(client: Sandbox) -> None:
    print("== Probe 1: browser_page.fill clear semantics ==")
    page = client.browser_page
    page.navigate(url=_FILL_PAGE)
    elements = page.get_elements().data or []
    index = None
    for el in elements:
        if isinstance(el, dict) and "input" in str(el.get("tag", "")).lower():
            index = el.get("index")
            break
    if index is None and elements:
        index = elements[0].get("index") if isinstance(elements[0], dict) else None
    if index is None:
        print("  !! no input element found on the probe page; elements:", elements)
        return
    page.fill(text="aaa", index=index)
    page.fill(text="bbb", index=index)
    result = page.evaluate(
        expression="document.getElementById('probe').value"
    )
    value = getattr(result, "data", None)
    print(f"  value after fill('aaa') then fill('bbb'): {value!r}")
    if value == "bbb":
        print("  => fill REPLACES (old clear:true semantics preserved) — OK")
    elif value == "aaabbb":
        print("  => fill APPENDS — REGRESSION: SdkBrowserBackend.type must clear first")
    else:
        print("  => inconclusive; inspect manually")


def _report_file_fault(label: str, fn) -> None:
    try:
        response = fn()
    except ApiError as exc:
        print(f"  {label}: HTTP-status channel (ApiError {exc.status_code}), body={exc.body!r}")
        return
    success = getattr(response, "success", None)
    message = getattr(response, "message", None)
    error_type = getattr(getattr(response, "data", None), "error_type", None)
    if success is False:
        print(
            f"  {label}: in-band channel (200 + success:false), "
            f"message={message!r}, error_type={error_type!r}"
        )
    else:
        print(f"  {label}: unexpectedly succeeded: success={success!r} message={message!r}")


def probe_file_fault_channel(client: Sandbox) -> None:
    print("== Probe 2: file fault channel (HTTP status vs 200+success:false) ==")
    _report_file_fault(
        "read missing file      ",
        lambda: client.file.read_file(file="/no/such/file/anywhere.txt"),
    )
    _report_file_fault(
        # /proc/version is a file, so it cannot be a parent directory.
        "write impossible path  ",
        lambda: client.file.write_file(file="/proc/version/x.txt", content="x"),
    )
    # download_file streams raw bytes with no JSON envelope, so it has no
    # in-band channel to answer on — only the HTTP status is left to classify.
    try:
        b"".join(client.file.download_file(path="/no/such/file/anywhere.txt"))
        print("  download missing file  : unexpectedly succeeded")
    except ApiError as exc:
        print(f"  download missing file  : ApiError {exc.status_code} (404 expected)")


def main() -> int:
    base_url = (
        sys.argv[1] if len(sys.argv) > 1 else os.environ.get("NOETA_TEST_AIO_SANDBOX_URL")
    )
    if not base_url:
        print(
            "usage: verify-sandbox-sdk-live.py <base_url> "
            "(or set NOETA_TEST_AIO_SANDBOX_URL)",
            file=sys.stderr,
        )
        return 2
    client = _client(base_url)
    try:
        version = getattr(client.sandbox.get_context().data, "version", None)
    except Exception:
        version = None
    print(f"target: {base_url}" + (f" (image version: {version})" if version else ""))
    probe_file_fault_channel(client)
    probe_fill_clear(client)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
