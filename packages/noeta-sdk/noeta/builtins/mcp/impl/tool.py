"""MCP tool wrapper + provider-safe name mapping.

Each tool a local stdio MCP server exposes becomes an
ordinary Noeta :class:`~noeta.protocols.tool.Tool` so it flows through the
one tool set into the composer schema, the policy, and the
``PermissionGuard`` with no special casing.

Naming: the Noeta-side tool name is
``mcp__{alias}__{safe_tool}`` where ``safe_tool`` is the raw MCP tool
name with every char outside ``[A-Za-z0-9_-]`` replaced by ``_``. The
full name must match ``^[A-Za-z0-9_-]{1,64}$`` (provider-safe); empty
raw names, empty post-sanitize names, over-64 names, and intra-server
sanitize collisions all **fail fast** (no silent truncation, no boundary
``mcp__alias__`` name). ``mcp__`` is a reserved prefix.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Optional

from collections.abc import Sequence
from typing import Union

from noeta.protocols.tool import ToolContext, ToolResult
from noeta.tools.limits import (
    INLINE_CONTENT_MAX_BYTES,
    MCP_INJECTION_MAX_BYTES,
    fit_output_fields,
    truncate_bytes,
)
from noeta.tools.refs import ref_json
from noeta.builtins.mcp.impl._client import McpStdioClient, SpawnFn
from noeta.builtins.mcp.impl._http_client import McpHttpClient
from noeta.runtime.mcp import (
    MCP_PREFIX,
    HttpPostFn,
    McpAnyServerSpec,
    McpConfigError,
    McpError,
    McpHttpServerSpec,
    McpServerSpec,
)


__all__ = [
    "MCP_PREFIX",
    "McpConfigError",
    "McpHttpServerSpec",
    "McpServerSkip",
    "McpServerSpec",
    "McpTool",
    "McpToolSpec",
    "build_mcp_tools",
    "cap_injected",
    "describe_mcp_tool",
    "is_mcp_tool_name",
    "make_mcp_tool_name",
    "parse_mcp_tool_specs",
]


def cap_injected(text: str, *, kind: str) -> str:
    """Bound server-controlled injected text at :data:`MCP_INJECTION_MAX_BYTES`.

    An MCP prompt / resource body is injected as an ``origin="system"``
    message, so an unbounded one is BOTH a prompt-injection surface and a
    context/token bomb (the transport only caps at ~8 MB). Injection has its
    OWN 64 KiB ceiling rather than the tool-result one: that ceiling grew to
    1 MiB for content the model asked for, while an injected turn is content
    nobody asked for that then sits in the history for the rest of the task.
    Past the cap the text is truncated with a visible marker naming ``kind``
    ("prompt" / "resource") so the model knows it was cut.

    Single shared implementation for :func:`~noeta.builtins.mcp.impl.prompts.
    flatten_prompt_messages` and :func:`~noeta.builtins.mcp.impl.resources.
    flatten_resource_contents` so the cap wording / ceiling never drift."""
    if len(text.encode("utf-8")) <= MCP_INJECTION_MAX_BYTES:
        return text
    return (
        truncate_bytes(text, MCP_INJECTION_MAX_BYTES)
        + f"\n\n[truncated: MCP {kind} exceeded {MCP_INJECTION_MAX_BYTES} bytes]"
    )


#: Cap on a server-supplied tool description. It rides the tool schema on
#: EVERY request for the rest of the session, so an essay is paid for forever;
#: 1 KiB fits any real description.
_MCP_DESCRIPTION_MAX_BYTES = 1024

#: Inline share of an over-cap MCP tool RESULT. Half the content ceiling, so
#: the marked head still fits after JSON-escape expansion instead of being
#: halved again by ``fit_output_fields``.
_MCP_RESULT_INLINE_MAX_BYTES = INLINE_CONTENT_MAX_BYTES // 2


def describe_mcp_tool(alias: str, description: object) -> str:
    """The model-facing description of one MCP tool: source first, then the
    server's own words, capped.

    The server writes this text and it lands in the tool list beside noeta's
    own tool descriptions, where a sentence like "always call this before
    anything else" reads as harness documentation. The prefix names whose
    sentence it is, at the one place a server's words enter the tool list.
    """
    text = description if isinstance(description, str) else ""
    if len(text.encode("utf-8")) > _MCP_DESCRIPTION_MAX_BYTES:
        text = (
            truncate_bytes(text, _MCP_DESCRIPTION_MAX_BYTES)
            + f"\n[truncated: description exceeded "
            f"{_MCP_DESCRIPTION_MAX_BYTES} bytes]"
        )
    prefix = f"[MCP server {alias!r}]"
    return f"{prefix} {text}" if text else prefix


_OUTPUT_MEDIA_TYPE = "application/json"
_NAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_UNSAFE_RE = re.compile(r"[^A-Za-z0-9_-]")


@dataclass(frozen=True, slots=True)
class McpToolSpec:
    """The minimal (name + input_schema + description) needed to rebuild the
    MCP tool set on resume.

    Extracted from a recording's first ``LLMRequest.tools`` (R-1), so the
    rebuilt tool set's schema + stable hash match the live recording
    without ever reconnecting to the server. ``description`` is captured
    verbatim for the same reason ``input_schema`` is: it rides
    in ``provider_tool_schemas`` and folds into the stable hash, so a resumed
    run must reproduce it exactly."""

    name: str
    input_schema: dict[str, Any] = field(default_factory=dict)
    description: str = ""


@dataclass(frozen=True, slots=True)
class McpServerSkip:
    """One enabled MCP server that ``build_mcp_tools`` could not connect.

    Returned (third element) only when ``skip_on_failure=True``: the server's
    connect / handshake / ``tools/list`` raised, so it was dropped and the build
    continued with the remaining servers. ``alias`` is the server's clean alias
    (never a url/token — credentials never leave the spec); ``reason`` is the
    typed fault message (``McpError`` / ``McpConfigError`` str). The caller (the
    SDK host / the CLI runner) turns each skip into one durable
    ``McpServerSkipped`` observer event the front-end surfaces."""

    alias: str
    reason: str


def mcp_provenance_from_specs(
    specs: Sequence[McpAnyServerSpec],
) -> list[dict[str, Any]]:
    """The per-task MCP provenance record from connect specs.

    Returns a deterministic, **credential-free** list of ``{"alias", "tools"}``
    dicts — one per enabled+resolved server, alias-sorted, each ``tools`` the
    server's ticked raw-name subset (sorted) or ``[]`` when no subset was set
    (⇒ all advertised tools). It records ONLY names: never a url / token / header
    (those live on the spec but are deliberately dropped here) — so the
    record is safe to persist in any event / recording / task provenance. The actual
    tool shape / behaviour is NOT carried here; the recorded
    ``request_ref`` tool spec (rebuilt on resume) is that truth. This is the audit
    answer to "what connectors + which of their tools was this task given this run".

    Lists (not tuples) so the JSON round-trip through the event log / snapshot
    is byte-stable (a tuple would deserialise back as a list and drift)."""
    out: list[dict[str, Any]] = []
    for spec in sorted(specs, key=lambda s: s.alias):
        subset = spec.tool_subset
        tools = sorted(subset) if subset is not None else []
        out.append({"alias": spec.alias, "tools": tools})
    return out


def is_mcp_tool_name(name: str) -> bool:
    return name.startswith(MCP_PREFIX)


def make_mcp_tool_name(alias: str, raw_tool_name: object) -> str:
    """Map a raw MCP tool name to the provider-safe Noeta-side name, or
    raise :class:`McpConfigError` (fail-closed)."""
    if not isinstance(raw_tool_name, str) or raw_tool_name == "":
        raise McpConfigError(
            f"MCP server {alias!r} advertised a tool with a missing/empty name"
        )
    safe = _UNSAFE_RE.sub("_", raw_tool_name)
    if safe == "":
        raise McpConfigError(
            f"MCP server {alias!r} tool name {raw_tool_name!r} sanitizes to empty"
        )
    name = f"{MCP_PREFIX}{alias}__{safe}"
    if not _NAME_RE.match(name):
        raise McpConfigError(
            f"MCP tool name {name!r} is not provider-safe "
            "(must match ^[A-Za-z0-9_-]{1,64}$ — too long?)"
        )
    return name


# ---------------------------------------------------------------------------
# Live tool
# ---------------------------------------------------------------------------


class McpTool:
    """A live MCP server tool exposed as a Noeta ``Tool``."""

    def __init__(
        self,
        *,
        name: str,
        remote_tool_name: str,
        input_schema: dict[str, Any],
        client: Union[McpStdioClient, McpHttpClient],
        risk_level: str = "high",
        description: str = "",
    ) -> None:
        self.name = name
        self.remote_tool_name = remote_tool_name
        self.input_schema = input_schema
        self.description = description
        self.risk_level = risk_level
        self._client = client

    def invoke(self, arguments: dict[str, Any], ctx: ToolContext) -> ToolResult:
        try:
            result = self._client.call_tool(self.remote_tool_name, arguments)
        except McpError as exc:
            return ToolResult(success=False, summary=f"{self.name}: {exc}")
        return _result_to_tool_result(self.name, result, ctx)


def _result_to_tool_result(
    tool_name: str, result: dict[str, Any], ctx: ToolContext
) -> ToolResult:
    """Map an MCP ``tools/call`` result to a Noeta ``ToolResult``.

    ``isError: true`` → ``success=False``. ``content`` text blocks are
    concatenated into ``output``; non-text blocks are summarised. A large
    ``output`` is offloaded to a ContentStore artifact (reusing the shared
    inline byte budget) and what stays inline is MARKED as a head — a silently
    shortened result reads as everything the server had to say, and the model
    answers from half an answer."""
    is_error = bool(result.get("isError"))
    content = result.get("content")
    text_parts: list[str] = []
    non_text = 0
    if isinstance(content, list):
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                t = block.get("text")
                if isinstance(t, str):
                    text_parts.append(t)
            else:
                non_text += 1
    text = "\n".join(text_parts)
    output: dict[str, Any] = {"text": text}
    if non_text:
        output["non_text_blocks"] = non_text
    artifacts = []
    encoded = text.encode("utf-8")
    if len(encoded) > INLINE_CONTENT_MAX_BYTES:
        ref = ctx.artifact_store.put(encoded, media_type="text/plain")
        artifacts.append(ref)
        # The marker leads the text rather than trailing it: every trim below
        # this point cuts from the END, so a trailing marker is the first thing
        # a second pass would drop.
        head = truncate_bytes(text, _MCP_RESULT_INLINE_MAX_BYTES)
        marked = (
            f"[truncated: the server returned {len(encoded)} bytes; what "
            f"follows is the first {len(head.encode('utf-8'))}]\n{head}"
        )
        output = fit_output_fields(
            {"text": marked, "text_ref": ref_json(ref), "non_text_blocks": non_text},
            shrink_order=["text"],
            max_bytes=INLINE_CONTENT_MAX_BYTES,
        )
    summary = (
        f"{tool_name}: {'error' if is_error else 'ok'} "
        f"({len(text)} text chars, {non_text} non-text block(s))"
    )
    return ToolResult(
        success=not is_error,
        output=output,
        summary=summary,
        artifacts=artifacts,
    )


# ---------------------------------------------------------------------------
# Build (live) — spawn, discover, map, collision-check, deterministic order
# ---------------------------------------------------------------------------


def _connect_client(
    spec: McpAnyServerSpec,
    *,
    spawn: Optional[SpawnFn] = None,
    http_post: Optional[HttpPostFn] = None,
) -> Union[McpStdioClient, McpHttpClient]:
    """Construct (not yet ``start``ed) the transport client for ``spec``.

    Dispatches on spec type: ``McpServerSpec`` → local stdio subprocess;
    ``McpHttpServerSpec`` → remote HTTP endpoint. Both expose the
    same ``start`` / ``list_tools`` / ``call_tool`` / ``shutdown`` surface so
    the build / wrap path below is transport-agnostic."""
    if isinstance(spec, McpHttpServerSpec):
        return McpHttpClient(
            url=spec.url, headers=spec.headers_dict(), post=http_post
        )
    return McpStdioClient(
        argv=list(spec.argv), env=spec.env_dict(), spawn=spawn
    )


def _discover_server_tools(
    spec: McpAnyServerSpec, client: Union[McpStdioClient, McpHttpClient]
) -> dict[str, McpTool]:
    """``tools/list`` on a started ``client`` → the subset-filtered, wrapped,
    name-sorted ``McpTool`` dict for ``spec``. Any ``tools/list`` / mapping /
    collision fault propagates (``McpError`` / ``McpConfigError``)."""
    # per-server tool subset (the user-chosen allow-list, raw
    # ``tools/list`` names). ``None`` ⇒ keep all (back-compat); a tuple ⇒
    # drop any advertised tool not in it BEFORE it is wrapped, so unselected
    # tools never enter the tool set / reach the model. The surviving set is
    # sorted below, so order/stable-hash determinism is unchanged.
    subset = spec.tool_subset
    allow = set(subset) if subset is not None else None
    built: dict[str, McpTool] = {}
    for raw in client.list_tools():
        raw_name = raw.get("name")
        if allow is not None and raw_name not in allow:
            continue
        noeta_name = make_mcp_tool_name(spec.alias, raw_name)
        if noeta_name in built:
            raise McpConfigError(
                f"MCP tool name collision on {noeta_name!r} "
                f"(server {spec.alias!r}): two raw names sanitize alike"
            )
        schema = raw.get("inputSchema")
        if not isinstance(schema, dict):
            schema = {"type": "object", "additionalProperties": True}
        built[noeta_name] = McpTool(
            name=noeta_name,
            remote_tool_name=str(raw_name),
            input_schema=schema,
            client=client,
            description=describe_mcp_tool(spec.alias, raw.get("description")),
        )
    return {noeta_name: built[noeta_name] for noeta_name in sorted(built)}


def _connect_one_server(
    spec: McpAnyServerSpec,
    *,
    spawn: Optional[SpawnFn],
    http_post: Optional[HttpPostFn],
    pool: Optional[Any],
    pool_scope: Optional[str] = None,
) -> tuple[
    dict[str, McpTool], Union[McpStdioClient, McpHttpClient]
]:
    """Connect ONE server, discover + wrap its (subset-filtered) tools.

    Returns ``(built_tools_sorted, client)``. Without a ``pool`` the client
    is fresh and the caller owns its ``shutdown``; with one it is acquired
    from the pool (shared with every other build naming the same server) and
    the caller owns its ``release``. Any connect / handshake / ``tools/list``
    / mapping / collision fault propagates (``McpError`` / ``McpConfigError``)
    with the client already shut down (fresh) or released (pooled).

    A pooled connection that was reused and then fails ``tools/list`` is
    retired and the server connected fresh ONCE before the fault propagates:
    a stdio server that died between turns, or an HTTP endpoint that was
    restarted, costs one reconnect rather than a turn without its tools. A
    ``McpConfigError`` (a tool-name collision) is the operator's wiring, not
    the connection's fault: the pooled client is released intact, never
    retired. ``pool_scope`` is the host's partition (see the pool module).
    """
    if pool is None:
        client = _connect_client(spec, spawn=spawn, http_post=http_post)
        try:
            client.start()
            built = _discover_server_tools(spec, client)
        except BaseException:
            # The partial connection of THIS server is dead — drop it.
            client.shutdown()
            raise
        return built, client
    client, reused = pool.acquire(spec, pool_scope)
    try:
        return _discover_server_tools(spec, client), client
    except McpConfigError:
        pool.release(client)
        raise
    except McpError:
        pool.retire(client)
        pool.release(client)
        if not reused:
            raise
    except BaseException:
        pool.retire(client)
        pool.release(client)
        raise
    client, _ = pool.acquire(spec, pool_scope)
    try:
        return _discover_server_tools(spec, client), client
    except McpConfigError:
        pool.release(client)
        raise
    except BaseException:
        pool.retire(client)
        pool.release(client)
        raise


def build_mcp_tools(
    specs: tuple[McpAnyServerSpec, ...],
    *,
    spawn: Optional[SpawnFn] = None,
    http_post: Optional[HttpPostFn] = None,
    skip_on_failure: bool = False,
    pool: Optional[Any] = None,
    pool_scope: Optional[str] = None,
) -> tuple[
    dict[str, McpTool],
    list[Union[McpStdioClient, McpHttpClient]],
    list[McpServerSkip],
]:
    """Connect each server, discover its tools, and build the namespaced
    ``McpTool`` set. Specs may be local stdio (``McpServerSpec``) or remote
    HTTP (``McpHttpServerSpec``); both map to the same ``mcp__{alias}__{tool}``
    tools. **Deterministic order**: servers in ``specs``
    order (callers pass them alias-sorted), tools within a server sorted by
    Noeta-side name — so the ``tools`` dict order → schema order → stable hash is
    reproducible on resume.

    Failure handling is governed by ``skip_on_failure``:

    * ``False`` (default — ``discover_tools`` / the CLI menu path): any connect /
      handshake / mapping / collision fault is **fail-fast** — it tears down every
      already-connected client and re-raises.
    * ``True`` (the task-start lifecycle path): a per-server fault is **caught**;
      the offending server is dropped (its partial client already shut down), a
      :class:`McpServerSkip` ``(alias, reason)`` is recorded, and the build
      continues with the remaining servers (option B — one bad connector never
      sinks the whole task). The caller turns each skip into a durable
      ``McpServerSkipped`` observer event the front-end surfaces.

    ``pool`` (an :class:`~noeta.builtins.mcp.impl.pool.McpConnectionPool`)
    is the task-start path's connection source: clients are acquired from it
    — shared with every other build naming the same server in the same
    ``pool_scope`` (the host's partition, ``None`` = shared), ``tools/list``
    run afresh for this build — and the caller owns their ``release``.
    Without a pool (discovery, the CLI menu) every client is fresh and the
    caller owns its ``shutdown``.

    Returns ``(tools, clients, skipped)``; ``skipped`` is always ``[]`` when
    ``skip_on_failure=False``. Returns ``({}, [], [])`` for empty ``specs`` so
    the default-off path constructs nothing.

    A **duplicate alias** is always a hard ``McpConfigError`` regardless of
    ``skip_on_failure`` — it is a caller wiring bug (the enabled-alias list / the
    config store must already be unique), not a per-server connect fault, so we
    never silently swallow it into a skip."""
    tools: dict[str, McpTool] = {}
    clients: list[Union[McpStdioClient, McpHttpClient]] = []
    skipped: list[McpServerSkip] = []
    if not specs:
        return tools, clients, skipped
    seen_aliases: set[str] = set()
    try:
        for spec in specs:
            if spec.alias in seen_aliases:
                raise McpConfigError(f"duplicate MCP server alias {spec.alias!r}")
            seen_aliases.add(spec.alias)
            try:
                built, client = _connect_one_server(
                    spec, spawn=spawn, http_post=http_post, pool=pool,
                    pool_scope=pool_scope,
                )
            except (McpError, McpConfigError) as exc:
                if not skip_on_failure:
                    raise
                # Skip-on-failure option: drop this server, record the skip, keep going.
                skipped.append(McpServerSkip(alias=spec.alias, reason=str(exc)))
                continue
            clients.append(client)
            tools.update(built)
    except BaseException:
        for c in clients:
            if pool is None:
                c.shutdown()
            else:
                pool.release(c)
        raise
    return tools, clients, skipped


# ---------------------------------------------------------------------------
# Resume — extract tool specs from a recorded request
# ---------------------------------------------------------------------------


def parse_mcp_tool_specs(request_tools: list[dict[str, Any]]) -> tuple[McpToolSpec, ...]:
    """Extract MCP tool specs from a recorded ``LLMRequest.tools`` array
    (R-1). Keeps every entry whose ``function.name`` is ``mcp__``-prefixed,
    **in recorded order**, with ``name`` + ``parameters`` verbatim. The
    ``spawn_subagent`` control schema is not ``mcp__``-prefixed, so it is
    never captured.

    Currently unwired in production: the live path builds MCP tools via
    :func:`build_mcp_tools`. ``parse_mcp_tool_specs`` (and the
    :class:`McpToolSpec` it returns) are a tested seam with no production
    caller — kept for reconstructing tool specs from a recorded
    ``LLMRequest.tools`` array."""
    out: list[McpToolSpec] = []
    for entry in request_tools:
        if not isinstance(entry, dict):
            continue
        fn = entry.get("function")
        if not isinstance(fn, dict):
            continue
        name = fn.get("name")
        if isinstance(name, str) and is_mcp_tool_name(name):
            params = fn.get("parameters")
            desc = fn.get("description")
            out.append(
                McpToolSpec(
                    name=name,
                    input_schema=params if isinstance(params, dict) else {},
                    description=desc if isinstance(desc, str) else "",
                )
            )
    return tuple(out)
