"""OTLP trace-export wiring: Settings resolution + end-to-end plumbing.

Span assembly, OTLP/HTTP JSON encoding, and sink batching live in the
runtime and are covered by the repo-root suite (tests/test_trace_export.py).
This suite covers only the app's side of the contract:

- Config resolution follows the opt-in policy: only OTLP_ENDPOINT /
  OTLP_HEADERS (plus the OTel-standard OTEL_EXPORTER_OTLP_HEADERS fallback)
  enable/shape export; the ambient OTel-standard endpoint envs never do.
- A configured endpoint makes the served app actually export spans, with the
  configured headers, and without leaking message bodies (the audit
  allowlist). Fully offline: the collector is a local fake HTTP server.
"""
from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx
import pytest

from noeta.agent.config import Settings
from noeta.agent.main import create_app
from tests.conftest import LiveServer, create_session, login, wait_status

_OTLP_VARS = (
    "OTLP_ENDPOINT",
    "OTLP_HEADERS",
    "OTEL_EXPORTER_OTLP_ENDPOINT",
    "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT",
    "OTEL_EXPORTER_OTLP_HEADERS",
)


def _settings(monkeypatch: pytest.MonkeyPatch, **env: str) -> Settings:
    """Settings from exactly ``env`` (ambient OTLP vars cleared, .env
    skipped) — resolution tests must not see the developer environment."""
    for var in _OTLP_VARS:
        monkeypatch.delenv(var, raising=False)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    return Settings(_env_file=None)


# -- config resolution --------------------------------------------------------


def test_otlp_export_is_off_by_default(monkeypatch) -> None:
    s = _settings(monkeypatch)
    assert s.otlp_endpoint == ""
    assert s.otlp_header_items == ()


def test_ambient_otel_endpoint_does_not_enable_export(monkeypatch) -> None:
    # A k8s operator / shared shell injecting the OTel-standard endpoint for
    # other apps must not silently start noeta exporting.
    s = _settings(
        monkeypatch,
        OTEL_EXPORTER_OTLP_ENDPOINT="http://ambient:4318",
        OTEL_EXPORTER_OTLP_TRACES_ENDPOINT="http://ambient:4318/v1/traces",
    )
    assert s.otlp_endpoint == ""


def test_otlp_endpoint_enables_and_standard_headers_ride_along(monkeypatch) -> None:
    # Only the app key enables export; the OTel-standard HEADERS var then
    # rides along, percent-decoded per the spec.
    s = _settings(
        monkeypatch,
        OTLP_ENDPOINT="http://mine/v1/traces",
        OTEL_EXPORTER_OTLP_HEADERS="authorization=Basic%20dXNlcg==,x-org=acme",
    )
    assert s.otlp_endpoint == "http://mine/v1/traces"
    assert dict(s.otlp_header_items) == {
        "authorization": "Basic dXNlcg==",
        "x-org": "acme",
    }


def test_app_otlp_headers_take_priority_over_ambient(monkeypatch) -> None:
    s = _settings(
        monkeypatch,
        OTLP_ENDPOINT="http://mine/v1/traces",
        OTLP_HEADERS="x-app=one",
        OTEL_EXPORTER_OTLP_HEADERS="x-ambient=two",
    )
    assert dict(s.otlp_header_items) == {"x-app": "one"}


def test_otlp_header_parsing_drops_malformed_pairs(monkeypatch) -> None:
    s = _settings(
        monkeypatch,
        OTLP_ENDPOINT="http://mine/v1/traces",
        OTLP_HEADERS="ok=1, =nokey ,novalue, spaced = a%20b ",
    )
    assert dict(s.otlp_header_items) == {"ok": "1", "spaced": "a b"}


# -- end-to-end: the served app exports to a local fake collector -------------


class _FakeCollector:
    """Minimal local OTLP/HTTP collector: records every POST (path, headers,
    body) and answers 200. Keeps the end-to-end test fully offline."""

    def __init__(self) -> None:
        received = self.received = []

        class _Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:  # noqa: N802 - http.server API
                length = int(self.headers.get("Content-Length", "0"))
                received.append(
                    (self.path, dict(self.headers), self.rfile.read(length))
                )
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b"{}")

            def log_message(self, *args) -> None:  # silence request logging
                pass

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self.url = f"http://127.0.0.1:{self._server.server_address[1]}/v1/traces"
        self._thread = threading.Thread(
            target=self._server.serve_forever, daemon=True
        )
        self._thread.start()

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()

    def spans(self) -> list[dict]:
        out: list[dict] = []
        for _, _, body in self.received:
            request = json.loads(body)
            for rs in request["resourceSpans"]:
                for ss in rs["scopeSpans"]:
                    out.extend(ss["spans"])
        return out


def test_served_app_exports_spans_with_headers(tmp_path, monkeypatch) -> None:
    """OTLP_ENDPOINT set → one mock turn produces real OTLP/HTTP exports:
    spans reach the collector (flushed by the shutdown drain), the configured
    auth header rides every request, and message bodies never leak."""
    collector = _FakeCollector()
    env = {
        # The conftest baseline (isolated data dir + offline mock, sandbox
        # off), minus the tool-surface switches this test does not need.
        "DATA_DIR": str(tmp_path / "data"),
        "LLM_PROVIDER": "mock",
        "DEV_LOGIN_ENABLED": "true",
        "SESSION_SECRET": "test-secret",
        "SANDBOX_ENABLED": "false",
        "MEMORY_CONSOLIDATION": "false",
        # The wiring under test.
        "OTLP_ENDPOINT": collector.url,
        "OTLP_HEADERS": "authorization=Bearer%20test-key",
    }
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    server = LiveServer(create_app(Settings()))
    try:
        base_url = server.start()
        goal = "OTLP_SECRET_GOAL: check the export wiring"
        with httpx.Client(base_url=base_url, timeout=10.0) as client:
            login(client)
            sid = create_session(client)
            resp = client.post(
                f"/api/v1/sessions/{sid}/messages", json={"content": goal}
            )
            assert resp.status_code == 202
            # The mock's first turn ends in a clarifying question → the LLM
            # call span is completed and buffered in the sink.
            wait_status(client, sid, {"waiting"})
    finally:
        # Lifespan shutdown → Client.shutdown → observer drain → sink close
        # flushes the buffered spans to the collector.
        server.stop()
    try:
        assert collector.received, "no OTLP export request reached the collector"
        for path, headers, _ in collector.received:
            assert path == "/v1/traces"
            assert headers.get("authorization") == "Bearer test-key"
            assert headers.get("Content-Type") == "application/json"
        assert any(
            span["name"].startswith("llm ") for span in collector.spans()
        ), [span["name"] for span in collector.spans()]
        # The exporter inherits the audit allowlist: goal text never leaves.
        blob = b"".join(body for _, _, body in collector.received).decode()
        assert "OTLP_SECRET_GOAL" not in blob
    finally:
        collector.close()
