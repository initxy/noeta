"""``PreviewGateway`` — the noeta-agent HTML-app preview gateway
(revised by the single-port amendment).

The ``open_app`` tool (SDK side, ``noeta.tools.app``) depends only on the narrow
:class:`noeta.tools.app.AppPreviewGateway` Protocol; this module is its concrete
implementation in the product layer.

**Single-port design (revision).** The preview originally ran on a
*second, independent port* so the iframe got a different origin (isolation) and
its ``/api`` calls stayed same-origin (no CORS). That broke in real deployments
where only noeta's main port is reachable (VM / port-forward / reverse proxy):
the second port's connections were refused. So the gateway no longer owns an
HTTP server. Instead the noeta **main** server (the one the browser already
reaches) routes ``/preview/<token>/...`` into this gateway via :meth:`route`.
Isolation is now carried by the iframe ``sandbox`` attribute (the iframe runs in
an opaque/null origin — it cannot touch the noeta UI even though they share an
origin), and the ``/api`` proxy answers null-origin ``fetch`` with permissive
CORS. The gateway here is therefore:

* **a thread-safe mount registry**: ``token -> {workspace_dir, app_rel,
  proxy_to, task_id}``. Each ``open_app`` mints an unguessable ``token``; the
  render URL is the **relative** path ``/preview/<token>/`` (resolved by the
  browser against the noeta origin, so always reachable). Mounts are pure runtime
  state (never persisted, never in replay); the host unmounts them by
  ``task_id`` at session end.
* **the static + ``/api`` proxy logic**, exposed as :meth:`route` returning a
  :class:`PreviewResponse` for the main server to send (no HTTP server of its
  own). ``/preview/<token>/api/<rest>`` is forwarded server-side to
  ``proxy_to/<rest>`` (method/query/body/headers preserved, upstream
  status/body/Content-Type returned verbatim) with an
  ``Access-Control-Allow-Origin: *`` header so the null-origin iframe can read
  it. No credentials are ever injected (v1 is unauthenticated, D5).
"""

from __future__ import annotations

import secrets
import threading
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from noeta.tools.app import AppMount
from noeta.tools.fs._workspace import WorkspaceEscape, WorkspaceRoot


__all__ = ["MountLimitExceeded", "PreviewGateway", "PreviewResponse"]


# The path prefix the noeta main server routes into this gateway.
_PREFIX = "/preview/"

# Suffix → Content-Type for static app assets. Unknown suffixes fall through to
# application/octet-stream.
_STATIC_CONTENT_TYPES: dict[str, str] = {
    ".html": "text/html; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".mjs": "application/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".map": "application/json; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".ico": "image/x-icon",
    ".txt": "text/plain; charset=utf-8",
}

_ENTRY = "index.html"

# Per-gateway mount ceiling: a runaway model could otherwise
# mint unbounded tokens. 64 is comfortably above the v1 "one active app slot per
# session" policy while still bounding the registry.
_DEFAULT_MOUNT_LIMIT = 64

# Upstream forward read timeout (seconds) for the same-origin /api proxy.
_PROXY_TIMEOUT = 30.0

# Request headers NOT forwarded to the upstream. We pass through everything the
# page set (so a runtime user-pasted auth token reaches the target — note: this
# forwards PAGE-SUPPLIED headers, it never injects noeta-stored credentials, so the
# ADR red line "no stored creds, none from the model / in HTML source" holds),
# EXCEPT: hop-by-hop / framing headers urllib must recompute (host, length,
# connection, …); ``accept-encoding`` (we don't decode gzip, so don't let the
# upstream compress); and ``origin``/``referer``/``cookie``, which are
# ``null``/meaningless from the sandboxed iframe and only risk tripping upstream
# CSRF checks.
_DROP_REQUEST_HEADERS = frozenset(
    {
        "host",
        "content-length",
        "connection",
        "keep-alive",
        "proxy-connection",
        "transfer-encoding",
        "te",
        "trailer",
        "upgrade",
        "accept-encoding",
        "origin",
        "referer",
        "cookie",
    }
)


def _content_type_for(name: str) -> str:
    suffix = "." + name.rsplit(".", 1)[-1].lower() if "." in name else ""
    return _STATIC_CONTENT_TYPES.get(suffix, "application/octet-stream")


# Single-port amendment — the app is served UNDER a path prefix
# (``/preview/<token>/``), but a model naturally writes ``fetch("/api/...")`` with
# a LEADING SLASH, which the browser resolves against the ORIGIN ROOT
# (``/api/...``) — escaping the prefix, missing the gateway, and 404ing on noeta's
# main server. Rather than force the model to get relative paths exactly right, we
# inject this tiny shim into served HTML: it rewrites any same-origin request to
# ``/api/...`` so it carries the mount prefix, transparently. ``__MOUNT_PREFIX__``
# is filled with ``/preview/<token>/`` per serve; the token is url-safe
# (``[A-Za-z0-9_-]``) so it needs no JS-string escaping. It patches both ``fetch``
# (string / URL / Request input) and ``XMLHttpRequest.open``, and is idempotent
# (skips paths already under the prefix), so relative ``api/...`` keeps working.
_API_SHIM_TEMPLATE = """<script data-noeta-api-shim>
(function () {
  var P = "__MOUNT_PREFIX__";
  function fix(u) {
    try {
      var url = new URL(u, document.baseURI);
      if (
        url.origin === location.origin &&
        (url.pathname === "/api" || url.pathname.indexOf("/api/") === 0) &&
        url.pathname.indexOf(P) !== 0
      ) {
        url.pathname = P + url.pathname.replace(/^\\/+/, "");
        return url.toString();
      }
    } catch (e) {}
    return u;
  }
  var of = window.fetch;
  if (of) {
    window.fetch = function (input, init) {
      try {
        if (typeof input === "string" || input instanceof URL) {
          input = fix(String(input));
        } else if (input && typeof input === "object" && input.url) {
          input = new Request(fix(input.url), input);
        }
      } catch (e) {}
      return of.call(this, input, init);
    };
  }
  var XHR = window.XMLHttpRequest;
  if (XHR && XHR.prototype && XHR.prototype.open) {
    var oo = XHR.prototype.open;
    XHR.prototype.open = function (m, u) {
      try { arguments[1] = fix(u); } catch (e) {}
      return oo.apply(this, arguments);
    };
  }
})();
</script>
"""


def _tag_insert_pos(lower_html: bytes, tag: bytes) -> Optional[int]:
    """Byte offset just past the first ``<tag ...>`` in ``lower_html``, or None."""
    pos = lower_html.find(tag)
    if pos == -1:
        return None
    gt = lower_html.find(b">", pos)
    return gt + 1 if gt != -1 else None


def _inject_api_shim(html: bytes, mount_prefix: str) -> bytes:
    """Splice the ``/api`` rewrite shim into ``html`` so it runs before app code.

    Prefer right after ``<head ...>`` (so it patches ``fetch`` before any
    subsequent script), else after ``<html ...>``, else prepend as a last resort.
    """
    shim = _API_SHIM_TEMPLATE.replace("__MOUNT_PREFIX__", mount_prefix).encode("utf-8")
    lower = html.lower()
    idx = _tag_insert_pos(lower, b"<head")
    if idx is None:
        idx = _tag_insert_pos(lower, b"<html")
    if idx is None:
        return shim + html
    return html[:idx] + shim + html[idx:]


@dataclass(frozen=True, slots=True)
class PreviewResponse:
    """What :meth:`PreviewGateway.route` hands back for the main server to send.

    ``cors=True`` ⇒ the sender adds ``Access-Control-Allow-Origin: *`` (+ the
    methods/headers preflight pair) — set for the ``/api`` proxy + its OPTIONS
    preflight, so the sandboxed null-origin iframe's ``fetch`` can read it.
    """

    status: int
    content_type: str
    body: bytes
    cors: bool = False


@dataclass(frozen=True, slots=True)
class _MountEntry:
    workspace_dir: Path
    app_rel: str
    proxy_to: str
    task_id: str


class MountLimitExceeded(RuntimeError):
    """Raised by :meth:`PreviewGateway.mount` when the registry is full."""


class PreviewGateway:
    """Mount registry + static/``/api`` routing for HTML-app preview.

    Public surface:

    * ``mount(*, workspace_dir, app_rel, proxy_to, task_id) -> AppMount`` —
      register a route, get its token + **relative** render URL (structurally
      satisfies the SDK ``AppPreviewGateway`` Protocol).
    * ``unmount_task(task_id) -> int`` — drop every mount owned by a task.
    * ``route(method, path, query, *, content_type, accept, body)
      -> Optional[PreviewResponse]`` — the noeta main server calls this for any
      request; ``None`` ⇒ not a preview path (caller falls through).
    """

    def __init__(self, *, mount_limit: int = _DEFAULT_MOUNT_LIMIT) -> None:
        self._mount_limit = mount_limit
        # Registry guarded by ``_lock`` (read on every request thread, written
        # by mount/unmount).
        self._lock = threading.Lock()
        self._mounts: dict[str, _MountEntry] = {}

    # -- registry ------------------------------------------------------------

    def mount(
        self,
        *,
        workspace_dir: Path,
        app_rel: str,
        proxy_to: str,
        task_id: str,
    ) -> AppMount:
        """Register a mount and return its token + **relative** render URL.

        ``workspace_dir`` is the session workspace root; ``app_rel`` the
        workspace-relative subdir holding ``index.html`` + assets; ``proxy_to``
        the ``/api`` forward target; ``task_id`` the owning task (the unmount
        key). The URL is the relative path ``/preview/<token>/`` — the browser
        resolves it against the noeta origin, so it is reachable wherever the noeta
        UI is (no second port, no host baked in).
        """
        token = secrets.token_urlsafe(16)
        entry = _MountEntry(
            workspace_dir=Path(workspace_dir),
            app_rel=app_rel,
            proxy_to=proxy_to.rstrip("/"),
            task_id=task_id,
        )
        with self._lock:
            # One active app slot per session (v1): re-``open_app`` from
            # the same task replaces its prior mount(s) rather than leaking them.
            # Evict BEFORE the limit check so a re-mount never trips the ceiling
            # and the global 64 limit can't be exhausted by one long task.
            stale = [t for t, e in self._mounts.items() if e.task_id == task_id]
            for tok in stale:
                del self._mounts[tok]
            if len(self._mounts) >= self._mount_limit:
                raise MountLimitExceeded(
                    f"preview gateway mount limit reached "
                    f"({self._mount_limit}); unmount before adding more"
                )
            self._mounts[token] = entry
        return AppMount(token=token, url=f"{_PREFIX}{token}/")

    def unmount_task(self, task_id: str) -> int:
        """Drop every mount owned by ``task_id``; return the count removed.

        Session/task teardown calls this. Mounts are runtime-only state, so there
        is nothing to persist — dropping the entry is the whole story; the old
        ``/preview/<token>/`` + ``/preview/<token>/api/*`` then 404.
        """
        with self._lock:
            doomed = [t for t, e in self._mounts.items() if e.task_id == task_id]
            for tok in doomed:
                del self._mounts[tok]
        return len(doomed)

    @property
    def mount_count(self) -> int:
        with self._lock:
            return len(self._mounts)

    def _lookup(self, token: str) -> Optional[_MountEntry]:
        with self._lock:
            return self._mounts.get(token)

    # -- routing (called by the noeta main server) ----------------------------

    def route(
        self,
        method: str,
        path: str,
        query: str,
        *,
        content_type: Optional[str] = None,
        accept: Optional[str] = None,
        body: Optional[bytes] = None,
        headers: Optional[dict[str, str]] = None,
    ) -> Optional[PreviewResponse]:
        """Handle a ``/preview/<token>/...`` request; ``None`` if not ours.

        Static asset → :meth:`_serve_static`; ``/api/*`` → :meth:`_proxy`
        (with an OPTIONS preflight short-circuit). Unknown token / bad path →
        a 404 ``PreviewResponse`` (still ours — don't fall through to noeta).

        ``headers`` is the page's full request-header set, forwarded to the
        upstream (minus :data:`_DROP_REQUEST_HEADERS`); ``content_type``/``accept``
        remain for direct callers/tests and are folded in if not already present.
        """
        if not path.startswith(_PREFIX):
            return None
        rest = path[len(_PREFIX) :]
        token, sep, subpath = rest.partition("/")
        if not token or not sep:
            # /preview or /preview/<token> with no trailing slash.
            return PreviewResponse(404, "text/plain; charset=utf-8", b"not found")

        is_api = subpath == "api" or subpath.startswith("api/")
        if is_api and method == "OPTIONS":
            # CORS preflight for the null-origin iframe's fetch. Answer it BEFORE
            # the unknown-token guard: a preflight against a just-expired/unknown
            # token must still carry CORS (a bare 404 surfaces as an opaque CORS
            # error to the sandboxed page); a clean 204 is the intended failure.
            return PreviewResponse(204, "text/plain; charset=utf-8", b"", cors=True)

        entry = self._lookup(token)
        if entry is None:
            return PreviewResponse(404, "text/plain; charset=utf-8", b"unknown app")

        # The /api/* proxy must be matched BEFORE the static branch: ``api`` is
        # also under /preview/<token>/, so order disambiguates them.
        if is_api:
            api_rest = subpath[len("api") :].lstrip("/")
            forward = {k: v for k, v in (headers or {}).items()}
            lowered = {k.lower() for k in forward}
            if content_type is not None and "content-type" not in lowered:
                forward["content-type"] = content_type
            if accept is not None and "accept" not in lowered:
                forward["accept"] = accept
            return self._proxy(method, entry, api_rest, query, forward, body)

        if method != "GET":
            return PreviewResponse(
                405, "text/plain; charset=utf-8", b"method not allowed"
            )
        return self._serve_static(entry, subpath, f"{_PREFIX}{token}/")

    # -- static serving ------------------------------------------------------

    def _serve_static(
        self, entry: _MountEntry, subpath: str, mount_prefix: str
    ) -> PreviewResponse:
        # Percent-decode the request subpath before the on-disk lookup: the
        # browser encodes asset names with spaces / '+' / non-ASCII chars (e.g.
        # ``my photo.png`` → ``my%20photo.png``), and a literal-name lookup would
        # 404 the real file. Decode FIRST, then resolve — ``WorkspaceRoot.resolve``
        # still collapses ``..`` and rejects escapes, so decoding ``%2e%2e`` here
        # cannot slip a traversal past the sandbox (decode→resolve, never reverse).
        rel = urllib.parse.unquote(subpath) if subpath else _ENTRY
        # Sandbox is the *app subdir*, not the whole workspace: a request for
        # ``../secret`` must not escape into sibling workspace files. Root the
        # ``WorkspaceRoot`` at ``workspace_dir/app_rel`` and resolve ``rel`` under
        # it — the sandbox collapses ``..`` and rejects symlink/absolute escapes;
        # any escape attempt 404s (don't leak why).
        try:
            app_root = WorkspaceRoot.from_path(
                Path(entry.workspace_dir) / entry.app_rel
            )
            target = app_root.resolve(rel)
        except WorkspaceEscape:
            return PreviewResponse(404, "text/plain; charset=utf-8", b"not found")
        if not target.is_file():
            return PreviewResponse(404, "text/plain; charset=utf-8", b"not found")
        try:
            body = target.read_bytes()
        except OSError:
            return PreviewResponse(404, "text/plain; charset=utf-8", b"not found")
        content_type = _content_type_for(target.name)
        # Inject the /api rewrite shim into HTML responses only (the on-disk file
        # is untouched — the file panel still shows the true source; only the
        # bytes the iframe receives carry the shim).
        if content_type.startswith("text/html"):
            body = _inject_api_shim(body, mount_prefix)
        return PreviewResponse(200, content_type, body)

    # -- same-origin /api proxy ---------------------------------------------

    def _proxy(
        self,
        method: str,
        entry: _MountEntry,
        api_rest: str,
        query: str,
        headers: dict[str, str],
        body: Optional[bytes],
    ) -> PreviewResponse:
        # v1 DEMO BOUNDARY (red line): this forwards to whatever
        # ``proxy_to`` the model passed inline, with NO SSRF allowlist and NO
        # injected credentials. Acceptable only for local single-user demos.
        # Hardening to non-demo targets MUST first add a "forward only to declared
        # sites/routes" allowlist (the OpenAPI spec is the natural source).
        target = entry.proxy_to + "/" + api_rest if api_rest else entry.proxy_to
        if query:
            target = f"{target}?{query}"

        req = urllib.request.Request(target, data=body, method=method)
        for name, value in headers.items():
            if name.lower() in _DROP_REQUEST_HEADERS:
                continue
            req.add_header(name, value)

        try:
            with urllib.request.urlopen(req, timeout=_PROXY_TIMEOUT) as upstream:
                status = upstream.status
                payload = upstream.read()
                ctype = upstream.headers.get("Content-Type", "application/octet-stream")
        except urllib.error.HTTPError as exc:
            status = exc.code
            payload = exc.read()
            ctype = exc.headers.get("Content-Type", "application/octet-stream")
        except (urllib.error.URLError, OSError, TimeoutError):
            return PreviewResponse(
                502,
                "application/json; charset=utf-8",
                b'{"error":"upstream unreachable"}',
                cors=True,
            )
        return PreviewResponse(status, ctype, payload, cors=True)
