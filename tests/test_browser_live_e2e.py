"""Live-container end-to-end for the sandbox browser subsystem (spec B8).

Starts a real AIO Sandbox container via Docker, serves a fixture HTML page
through a host-reachable http server, and drives the real
:class:`~noeta.tools.browser.AioBrowserBackend` through all five noeta-owned
browser verbs — navigate / extract / type / click / screenshot — plus pins the
container's ``/mcp`` browser tool names against our backend constants so a
wire drift fails loudly here rather than perturbing the model-facing schema.

Gated: skipped unless ``NOETA_TEST_AIO_BROWSER=1`` is set (needs a local Docker
daemon + the AIO Sandbox image). Set ``NOETA_TEST_AIO_IMAGE`` to override the
image (default ``ghcr.io/agent-infra/sandbox:latest``; this repo's local dev
image may differ — see ``apps/noeta-agent/noeta/agent/host/docker_sandbox.py``).

This is the acceptance-criteria #8 test the implementation spec flagged as the
one place runtime return shapes + live tool names get pinned (the fake-transport
contract tests in ``test_browser_backend.py`` assert only what we *send*, not
what the live server *returns*).
"""

from __future__ import annotations

import os
import shutil
import socket
import subprocess
import tempfile
import threading
import time
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

import pytest

from noeta.tools.browser import (
    BROWSER_TOOL_NAMES,
    AioBrowserBackend,
    AioBrowserError,
)
from noeta.tools.mcp._http_client import McpHttpClient


#: Env var that gates this whole module.
_GATE_ENV = "NOETA_TEST_AIO_BROWSER"
#: Image override env (default matches the project's per-session sandbox default).
_IMAGE_ENV = "NOETA_TEST_AIO_IMAGE"
_DEFAULT_IMAGE = "ghcr.io/agent-infra/sandbox:latest"
#: Container-internal port every AIO service fronts on.
_CONTAINER_PORT = 8080
#: How long to wait for the container to serve ``/v1/sandbox`` 2xx.
_READY_TIMEOUT_S = 90.0
_READY_INTERVAL_S = 0.5


pytestmark = pytest.mark.skipif(
    not os.environ.get(_GATE_ENV),
    reason=f"set {_GATE_ENV}=1 to run live-container browser e2e (needs Docker + AIO image)",
)


# --------------------------------------------------------------------------- #
# Fixture page + host http server
# --------------------------------------------------------------------------- #

FIXTURE_HTML = """<!doctype html><html><body>
<h1>Noeta Browser E2E Fixture</h1>
<p>This is a fixture page for testing browser tools.</p>
<a href="result.html" id="thelink">Click me to result</a>
<form action="result.html" method="get">
  <label>Search: <input type="text" name="q" id="q"></label>
  <button type="submit">Go</button>
</form>
</body></html>
"""

RESULT_HTML = """<!doctype html><html><body>
<h1>Result Page</h1><p id="out">You made it.</p></body></html>
"""


def _pick_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


class _FixtureServer:
    """Serves the fixture HTML on a host port, bound to 0.0.0.0 so the container
    can reach it via ``host.docker.internal`` (mapped to the docker gateway)."""

    def __init__(self, fixture_dir: str) -> None:
        self.port = _pick_free_port()
        handler = SimpleHTTPRequestHandler
        self._server = ThreadingHTTPServer(("0.0.0.0", self.port), handler)
        # serve from the fixture dir (chdir is done by the caller / we set directory)
        # ThreadingHTTPServer with SimpleHTTPRequestHandler serves cwd; we'll chdir
        # before starting.
        self._thread: threading.Thread | None = None
        self._fixture_dir = fixture_dir

    def start(self) -> None:
        import os as _os

        _os.chdir(self._fixture_dir)  # noqa: PTH108 — server serves cwd
        self._thread = threading.Thread(
            target=self._server.serve_forever, daemon=True, name="fixture-http"
        )
        self._thread.start()
        # quick self-check via 127.0.0.1
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            try:
                with socket.create_connection(("127.0.0.1", self.port), timeout=1.0):
                    return
            except OSError:
                time.sleep(0.05)
        raise RuntimeError("fixture http server did not bind")

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()


@pytest.fixture(scope="module")
def fixture_server() -> _FixtureServer:
    with tempfile.TemporaryDirectory() as td:
        with open(os.path.join(td, "page.html"), "w") as f:
            f.write(FIXTURE_HTML)
        with open(os.path.join(td, "result.html"), "w") as f:
            f.write(RESULT_HTML)
        srv = _FixtureServer(td)
        srv.start()
        try:
            yield srv
        finally:
            srv.stop()


# --------------------------------------------------------------------------- #
# Container lifecycle
# --------------------------------------------------------------------------- #


def _docker_available() -> bool:
    return shutil.which("docker") is not None


def _await_ready(base_url: str, key: str) -> None:
    import urllib.error
    import urllib.request

    deadline = time.monotonic() + _READY_TIMEOUT_S
    while time.monotonic() < deadline:
        try:
            req = urllib.request.Request(  # noqa: S310
                base_url + "/v1/sandbox",
                headers={"X-AIO-API-Key": key},
                method="GET",
            )
            with urllib.request.urlopen(req, timeout=2.0) as resp:  # noqa: S310
                if 200 <= resp.status < 300:
                    return
        except (urllib.error.URLError, OSError):
            pass
        time.sleep(_READY_INTERVAL_S)
    raise TimeoutError(
        f"AIO sandbox at {base_url} did not serve /v1/sandbox within {_READY_TIMEOUT_S:.0f}s"
    )


@pytest.fixture(scope="module")
def live_sandbox() -> dict:
    """Provision a real AIO container; yield ``{base_url, key, container_name}``.

    Skipped (module-collection skip via pytestmark above gates the whole file,
    but we also check Docker here for a clear error)."""
    if not _docker_available():
        pytest.skip("docker not found on PATH")
    image = os.environ.get(_IMAGE_ENV, _DEFAULT_IMAGE)
    api_key = f"noeta-e2e-{os.getpid()}-{int(time.time())}"
    port = _pick_free_port()
    container = f"noeta-browser-e2e-{os.getpid()}-{int(time.time())}"
    argv = [
        "docker", "run", "-d",
        "--name", container,
        "-p", f"127.0.0.1:{port}:{_CONTAINER_PORT}",
        "--add-host=host.docker.internal:host-gateway",
        "--security-opt", "seccomp=unconfined",
        "--memory", "2g", "--cpus", "2",
        "-e", f"SANDBOX_API_KEY={api_key}",
        image,
    ]
    run_env = {**os.environ, "SANDBOX_API_KEY": api_key}
    try:
        result = subprocess.run(  # noqa: S603
            argv, capture_output=True, text=True, check=False, env=run_env,
        )
    except FileNotFoundError:
        pytest.skip("docker binary not found")
        return {}
    if result.returncode != 0:
        pytest.skip(f"docker run failed (image {image!r}?): {result.stderr.strip()}")
        return {}
    base_url = f"http://127.0.0.1:{port}"
    try:
        _await_ready(base_url, api_key)
    except TimeoutError as exc:
        subprocess.run(["docker", "rm", "-f", container], capture_output=True, check=False)  # noqa: S603
        pytest.skip(f"container did not become ready: {exc}")
        return {}
    info = {"base_url": base_url, "key": api_key, "container": container}
    try:
        yield info
    finally:
        subprocess.run(["docker", "rm", "-f", container], capture_output=True, check=False)  # noqa: S603


@pytest.fixture(scope="module")
def browser_backend(live_sandbox: dict) -> AioBrowserBackend:
    return AioBrowserBackend(
        base_url=live_sandbox["base_url"],
        auth_headers=lambda: {"X-AIO-API-Key": live_sandbox["key"]},
        timeout_s=60.0,
    )


# --------------------------------------------------------------------------- #
# Tests
# --------------------------------------------------------------------------- #


class TestLiveWireNames:
    """Pin the live container's ``/mcp`` browser tool names against our backend
    constants so an image upgrade that renames a tool fails here (R1)."""

    def test_browser_tool_names_present(self, live_sandbox: dict) -> None:
        client = McpHttpClient(
            url=live_sandbox["base_url"] + "/mcp",
            headers={"X-AIO-API-Key": live_sandbox["key"]},
            timeout_s=30.0,
        )
        client.start()
        live_names = {t["name"] for t in client.list_tools()}
        # The seven AIO tools our backend maps to (the wire constants).
        expected_aio = {
            "browser_navigate",
            "browser_click",
            "browser_form_input_fill",
            "browser_press_key",
            "browser_get_markdown",
            "browser_get_clickable_elements",
            "browser_screenshot",
        }
        missing = expected_aio - live_names
        assert not missing, (
            f"AIO image is missing browser tools our backend expects: {sorted(missing)}. "
            "Wire constants in noeta.tools.browser._backend need re-pinning."
        )

    def test_noeta_tool_names_are_ours(self) -> None:
        """Sanity: the noeta-owned model-facing names are the fixed five."""
        assert BROWSER_TOOL_NAMES == (
            "browser_navigate",
            "browser_click",
            "browser_type",
            "browser_extract",
            "browser_screenshot",
        )


class TestLiveVerbs:
    """Drive the real ``AioBrowserBackend`` against the fixture page."""

    @pytest.fixture(scope="class")
    def fixture_url(self, fixture_server: _FixtureServer) -> str:
        return f"http://host.docker.internal:{fixture_server.port}/page.html"

    def test_navigate(self, browser_backend: AioBrowserBackend, fixture_url: str) -> None:
        snapshot = browser_backend.navigate(fixture_url)
        # navigate returns inline clickable elements with the fixture content.
        assert "Noeta Browser E2E Fixture" in snapshot
        assert "Click me to result" in snapshot
        # element indices are rendered as ``[N]<tag>...``
        assert "<a>" in snapshot

    def test_extract(self, browser_backend: AioBrowserBackend, fixture_url: str) -> None:
        # ensure we're on the fixture
        browser_backend.navigate(fixture_url)
        snapshot = browser_backend.extract()
        # page markdown text
        assert "Noeta Browser E2E Fixture" in snapshot
        # the composed interactive-elements section
        assert "# Interactive elements" in snapshot
        assert "<input>" in snapshot  # the search input is listed

    def test_type_submit(self, browser_backend: AioBrowserBackend, fixture_url: str) -> None:
        browser_backend.navigate(fixture_url)
        ext = browser_backend.extract()
        # find the input index in the clickable list
        input_idx = None
        for line in ext.split("\n"):
            s = line.strip()
            if s.startswith("[") and "]<input" in s:
                try:
                    input_idx = int(s.split("]", 1)[0].lstrip("["))
                    break
                except ValueError:
                    pass
        assert input_idx is not None, f"no <input> found in clickable list; got:\n{ext}"
        result = browser_backend.type(input_idx, "noeta-e2e", submit=True)
        assert "Successfully filled" in result or "filled" in result.lower()
        assert "Enter" in result
        # form submitted → we should be on result.html
        after = browser_backend.extract()
        assert "Result Page" in after or "You made it" in after

    def test_click(self, browser_backend: AioBrowserBackend, fixture_url: str) -> None:
        browser_backend.navigate(fixture_url)
        ext = browser_backend.extract()
        link_idx = None
        for line in ext.split("\n"):
            s = line.strip()
            if s.startswith("[") and "]<a" in s:
                try:
                    link_idx = int(s.split("]", 1)[0].lstrip("["))
                    break
                except ValueError:
                    pass
        assert link_idx is not None, f"no <a> found in clickable list; got:\n{ext}"
        result = browser_backend.click(link_idx)
        assert "Clicked" in result or "clicked" in result.lower()
        after = browser_backend.extract()
        assert "Result Page" in after or "You made it" in after

    def test_screenshot(self, browser_backend: AioBrowserBackend, fixture_url: str) -> None:
        browser_backend.navigate(fixture_url)
        png = browser_backend.screenshot()
        assert isinstance(png, bytes) and len(png) > 0
        # PNG magic
        assert png[:8] == b"\x89PNG\r\n\x1a\n", f"not a PNG: {png[:16]!r}"
        assert len(png) > 500, f"suspiciously small screenshot: {len(png)} bytes"

    def test_backend_error_propagation(self, browser_backend: AioBrowserBackend) -> None:
        """A browser-level fault surfaces as ``AioBrowserError`` (an OSError),
        never crashes the worker."""
        with pytest.raises(AioBrowserError):
            # navigate to a scheme the container browser cannot resolve.
            browser_backend.navigate("http://invalid.invalid.example.nxdomain/page")
