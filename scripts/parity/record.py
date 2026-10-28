"""Recording reverse proxy: Claude Code -> here -> an upstream Messages API.

Every request/response pair is written to ``OUT/<nnnn>-<path>.json``:
``method``, ``path``, ``headers`` (credentials redacted), ``body`` (the exact
JSON Claude Code sent, before any rewrite), ``status`` and ``response`` (the
raw SSE or JSON text). A request the client abandons mid-stream (an
interrupt) is still written, with ``"interrupted": true`` and the partial
response.

    python -I scripts/parity/record.py OUT --port 18801 \\
        [--upstream https://gateway.example] [--model cheap-model] \\
        [--effort medium=high] [--auth-env PARITY_UPSTREAM_TOKEN] [--proxy URL]

``--model`` swaps the model on the way out only, so Claude Code still
believes it talks to the baseline model and sends the baseline's request.
``--effort`` remaps ``output_config.effort`` the same way (a cheap model may
not accept every level). ``--auth-env`` names an environment variable holding
the upstream token; it replaces the client's credentials, so Claude Code can
run with a placeholder token. Needs ``httpx``.
"""

from __future__ import annotations

import argparse
import json
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import httpx

SECRET = {"authorization", "x-api-key", "cookie"}
HOP = {"host", "content-length", "connection", "accept-encoding", "transfer-encoding"}


class Recorder:
    def __init__(self, out: Path, upstream: str, *, model: str | None = None,
                 effort: dict[str, str] | None = None, token: str | None = None,
                 proxy: str | None = None) -> None:
        self.out = out
        self.upstream = upstream.rstrip("/")
        self.model = model
        self.effort = effort or {}
        self.token = token
        self.client = httpx.Client(timeout=600, proxy=proxy or None, trust_env=False)
        self.lock = threading.Lock()
        self.counter = len(list(out.glob("*.json")))
        #: Requests in flight -> bytes streamed so far, mirrored to ``OUT/.live``
        #: so a driver can tell a busy client from an idle one.
        self.live: dict[int, int] = {}
        self.drop_disabled_thinking = False

    def next_index(self) -> int:
        with self.lock:
            self.counter += 1
            self.live[self.counter] = 0
            self._write_live()
            return self.counter

    def progress(self, idx: int, n: int | None) -> None:
        """``n`` more bytes streamed for ``idx``; ``None``: it finished."""
        with self.lock:
            if n is None:
                self.live.pop(idx, None)
            else:
                self.live[idx] = self.live.get(idx, 0) + n
            self._write_live()

    def _write_live(self) -> None:
        (self.out / ".live").write_text(json.dumps({str(k): v for k, v in self.live.items()}))

    def outbound(self, body: Any) -> tuple[Any, list[str]]:
        """The body as sent upstream (model and effort rewritten, and what
        the upstream model rejects adjusted) and the rewrites applied."""
        if not isinstance(body, dict) or "model" not in body:
            return body, []
        fwd = dict(body)
        done = []
        if self.model:
            fwd["model"] = self.model
            done.append("model")
        oc = fwd.get("output_config")
        if isinstance(oc, dict) and oc.get("effort") in self.effort:
            fwd["output_config"] = {**oc, "effort": self.effort[oc["effort"]]}
            done.append("effort")
        th = fwd.get("thinking")
        if self.drop_disabled_thinking and isinstance(th, dict) and th.get("type") == "disabled":
            del fwd["thinking"]
            done.append("thinking-disabled-dropped")
        return fwd, done

    def headers_out(self, headers: dict[str, str]) -> dict[str, str]:
        if not self.token:
            return headers
        out = {k: v for k, v in headers.items() if k.lower() not in ("authorization", "x-api-key")}
        out["authorization"] = f"Bearer {self.token}"
        return out


def handler(rec: Recorder) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *args: Any) -> None:
            pass

        def _forward(self) -> None:
            n = int(self.headers.get("content-length") or 0)
            raw = self.rfile.read(n) if n else b""
            idx = rec.next_index()
            headers = {k: v for k, v in self.headers.items() if k.lower() not in HOP}
            entry: dict[str, Any] = {
                "method": self.command,
                "path": self.path,
                "headers": {k: ("<redacted>" if k.lower() in SECRET else v)
                            for k, v in headers.items()},
            }
            body: Any = None
            try:
                body = json.loads(raw) if raw else None
                entry["body"] = body
            except ValueError:
                entry["body_raw"] = raw.decode("utf-8", "replace")
            fwd, rewrites = rec.outbound(body)
            if fwd is not body:
                raw = json.dumps(fwd).encode()
            if rewrites:
                entry["upstream_rewrites"] = rewrites
            chunks: list[bytes] = []
            try:
                req = rec.client.build_request(self.command, rec.upstream + self.path,
                                               headers=rec.headers_out(headers), content=raw)
                resp = rec.client.send(req, stream=True)
                entry["status"] = resp.status_code
                self.send_response(resp.status_code)
                for k, v in resp.headers.items():
                    if k.lower() in HOP or k.lower() == "content-encoding":
                        continue
                    self.send_header(k, v)
                self.send_header("transfer-encoding", "chunked")
                self.end_headers()
                try:
                    for chunk in resp.iter_bytes():
                        chunks.append(chunk)
                        rec.progress(idx, len(chunk))
                        self.wfile.write(b"%x\r\n%s\r\n" % (len(chunk), chunk))
                        self.wfile.flush()
                    self.wfile.write(b"0\r\n\r\n")
                finally:
                    resp.close()
            except (BrokenPipeError, ConnectionResetError):
                entry["interrupted"] = True
            finally:
                rec.progress(idx, None)
                entry["response"] = b"".join(chunks).decode("utf-8", "replace")
                name = self.path.split("?")[0].strip("/").replace("/", "_") or "root"
                (rec.out / f"{idx:04d}-{name}.json").write_text(
                    json.dumps(entry, ensure_ascii=False, indent=1), "utf-8")

        do_POST = do_GET = _forward

    return Handler


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description="Record Claude Code's API traffic.")
    ap.add_argument("out", type=Path)
    ap.add_argument("--port", type=int, required=True)
    ap.add_argument("--upstream", default="https://api.anthropic.com")
    ap.add_argument("--model", help="model sent upstream instead of the requested one")
    ap.add_argument("--effort", action="append", default=[], metavar="FROM=TO",
                    help="remap output_config.effort upstream (repeatable)")
    ap.add_argument("--auth-env", help="environment variable holding the upstream token")
    ap.add_argument("--proxy", help="HTTP proxy for the upstream connection")
    ap.add_argument("--drop-disabled-thinking", action="store_true",
                    help="forward thinking={type: disabled} without the field (for an upstream "
                         "model that rejects it)")
    args = ap.parse_args(argv)
    args.out.mkdir(parents=True, exist_ok=True)
    token = os.environ.get(args.auth_env) if args.auth_env else None
    if args.auth_env and not token:
        raise SystemExit(f"record: ${args.auth_env} is empty")
    rec = Recorder(args.out, args.upstream, model=args.model,
                   effort=dict(e.split("=", 1) for e in args.effort), token=token,
                   proxy=args.proxy)
    rec.drop_disabled_thinking = args.drop_disabled_thinking
    print(f"recording to {args.out} on 127.0.0.1:{args.port} -> {rec.upstream}", flush=True)
    ThreadingHTTPServer(("127.0.0.1", args.port), handler(rec)).serve_forever()


if __name__ == "__main__":
    main()
