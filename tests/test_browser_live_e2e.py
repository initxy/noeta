"""Live-container end-to-end for the sandbox subsystem (browser + ExecEnv).

Starts a real AIO Sandbox container via Docker, serves a fixture HTML page from
a host-reachable http server, and drives BOTH container adapters the ``sandbox``
built-in owns:

* :class:`~noeta.builtins.sandbox.impl.browser.AioBrowserBackend` through all
  five noeta-owned browser verbs — navigate / extract / type / click /
  screenshot — plus pinning the container's ``/mcp`` browser tool names against
  the backend's wire constants, so an image that renames a tool fails loudly
  here instead of quietly perturbing the model-facing schema.
* :class:`~noeta.builtins.sandbox.impl.exec_env.AioSandboxExecEnv` through its
  file IO (``/v1/file/{read,write}``) and process execution (``/v1/shell/exec``)
  against the same container — the half the fake-transport tests in
  ``test_aio_sandbox_exec_env.py`` cannot prove, since they assert only what we
  *send*, never what a real container *returns*.

This is the only place live return shapes and live tool names are pinned.

Gated: skipped unless ``NOETA_TEST_AIO_BROWSER=1`` is set (needs a local Docker
daemon and the AIO Sandbox image). ``NOETA_TEST_AIO_IMAGE`` overrides the image
(default ``ghcr.io/agent-infra/sandbox:latest``).
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
from pathlib import Path

import pytest

from noeta.builtins.sandbox.impl.browser import AioBrowserBackend, AioBrowserError
from noeta.builtins.sandbox.impl.exec_env import AioSandboxExecEnv
from noeta.builtins.browser.impl import BROWSER_TOOL_NAMES
from noeta.builtins.mcp.impl._http_client import McpHttpClient


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


def _element_index(extract_text: str, tag: str) -> int | None:
    """The leading ``[N]`` index of the first clickable line whose tag matches.

    The container renders each interactive element as ``[N] [descriptor] <tag>…``
    — the descriptor and the spacing around ``<tag>`` are the image's format, not
    noeta's (``AioBrowserBackend.extract`` passes the list through verbatim), so
    match the ``<tag>`` anywhere on the line rather than pinning ``]<tag``. ``tag``
    is a prefix like ``"<a"`` or ``"<input"``.
    """
    for line in extract_text.split("\n"):
        s = line.strip()
        if not s.startswith("["):
            continue
        head = s[1 : s.find("]")] if "]" in s else ""
        if tag in s and head.strip().isdigit():
            return int(head.strip())
    return None


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
        # SimpleHTTPRequestHandler serves the process cwd, so ``start`` chdirs
        # into the fixture dir before the thread comes up.
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

    ``pytestmark`` already gates the file on the env var; the Docker probe here
    turns a missing daemon into a readable skip instead of a spawn traceback."""
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
    """Pin the live container's ``/mcp`` browser tool names against the backend's
    wire constants, so an image upgrade that renames a tool fails here."""

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
            "Wire constants in noeta.builtins.sandbox.impl.browser need re-pinning."
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
        input_idx = _element_index(ext, "<input")
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
        link_idx = _element_index(ext, "<a")
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


# --------------------------------------------------------------------------- #
# Sandbox ExecEnv — the file + shell half of the same container
# --------------------------------------------------------------------------- #


@pytest.fixture(scope="module")
def exec_env(live_sandbox: dict) -> AioSandboxExecEnv:
    return AioSandboxExecEnv(
        base_url=live_sandbox["base_url"],
        auth_headers=lambda: {"X-AIO-API-Key": live_sandbox["key"]},
        timeout_s=60.0,
    )


class TestLiveExecEnv:
    """Drive the real ``AioSandboxExecEnv`` (file IO + shell) against the container.

    The fake-transport contract in ``test_aio_sandbox_exec_env.py`` pins what we
    *send*; only a real container proves the write→read round-trip, the shell
    exit code / stdout, and the in-band file-fault → ``OSError`` refinement.
    """

    def test_write_then_read_round_trips(self, exec_env: AioSandboxExecEnv) -> None:
        path = Path("/tmp/noeta_execenv_e2e.txt")
        body = b"ahoy from the container\n"
        exec_env.write_bytes(path, body)
        # Bytes and text views both reconstruct what we wrote. This is the
        # regression gate for the read contract: /v1/file/read is text-only
        # (FileReadRequest has no ``encoding`` field — v1.11.0 OpenAPI), so
        # ``read_bytes`` must ride the shell ``base64`` path, and an adapter
        # that base64-decodes the text endpoint's response corrupts every read.
        assert exec_env.read_bytes(path) == body
        assert exec_env.read_text(path) == body.decode("utf-8")

    def test_binary_write_then_read_round_trips(
        self, exec_env: AioSandboxExecEnv
    ) -> None:
        # Byte fidelity for content the text endpoint cannot represent at all:
        # NULs, invalid UTF-8, and enough volume that a truncated-inline-echo
        # (spill) response, if the container chooses one, must also round-trip.
        path = Path("/tmp/noeta_execenv_e2e.bin")
        body = bytes(range(256)) * 512  # 128 KiB, every byte value
        exec_env.write_bytes(path, body)
        assert exec_env.read_bytes(path) == body

    def test_shell_exec_reports_stdout_and_exit(self, exec_env: AioSandboxExecEnv) -> None:
        outcome = exec_env.run_argv(
            ["python3", "-c", "print(6*7)"],
            cwd=Path("/tmp"),
            timeout_s=30,
            output_cap=64 * 1024,
        )
        assert outcome.returncode == 0
        assert not outcome.timed_out
        assert b"42" in outcome.stdout

    def test_shell_nonzero_exit_is_reported_not_raised(
        self, exec_env: AioSandboxExecEnv
    ) -> None:
        outcome = exec_env.run_argv(
            ["python3", "-c", "import sys; sys.exit(3)"],
            cwd=Path("/tmp"),
            timeout_s=30,
            output_cap=64 * 1024,
        )
        # A controlled non-zero exit is data, not an exception.
        assert outcome.returncode == 3
        assert not outcome.timed_out

    def test_read_missing_file_maps_to_filenotfounderror(
        self, exec_env: AioSandboxExecEnv
    ) -> None:
        # Both read paths refine a missing file into the stdlib subclass the
        # local backend raises: read_bytes via its shell stat guard,
        # read_text via the endpoint's in-band error_type=not_found.
        missing = Path("/tmp/noeta_does_not_exist_e2e.txt")
        with pytest.raises(FileNotFoundError):
            exec_env.read_bytes(missing)
        with pytest.raises(FileNotFoundError):
            exec_env.read_text(missing)
