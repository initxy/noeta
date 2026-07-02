"""MCP tool wrapper + provider-safe name mapping.

Phase 4.5 F2. Each tool a local stdio MCP server exposes becomes an
ordinary Noeta :class:`~noeta.protocols.tool.Tool` so it flows through the
one tool set into the composer schema, the policy, and the
``PermissionGuard`` with no special casing.

Naming (architect-pinned, F2 rev2 §0): the Noeta-side tool name is
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
from noeta.tools._limits import (
    INLINE_CONTENT_MAX_BYTES,
    fit_output_fields,
    truncate_bytes,
)
from noeta.tools._refs import ref_json
from noeta.tools.mcp._client import McpError, McpStdioClient, SpawnFn
from noeta.tools.mcp._http_client import HttpPostFn, McpHttpClient


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
    "is_mcp_tool_name",
    "make_mcp_tool_name",
    "parse_mcp_tool_specs",
]


def cap_injected(text: str, *, kind: str) -> str:
    """Bound server-controlled injected text at the inline-content ceiling.

    An MCP prompt / resource body is injected as an ``origin="system"``
    message, so an unbounded one is BOTH a prompt-injection surface and a
    context/token bomb (the transport only caps at ~8 MB). 64 KiB is ample for a
    real prompt / resource snapshot; past it we truncate with a visible marker
    naming ``kind`` ("prompt" / "resource") so the model knows it was cut.

    Single shared implementation for :func:`~noeta.tools.mcp.prompts.
    flatten_prompt_messages` and :func:`~noeta.tools.mcp.resources.
    flatten_resource_contents` so the cap wording / ceiling never drift."""
    if len(text.encode("utf-8")) <= INLINE_CONTENT_MAX_BYTES:
        return text
    return (
        truncate_bytes(text, INLINE_CONTENT_MAX_BYTES)
        + f"\n\n[truncated: MCP {kind} exceeded {INLINE_CONTENT_MAX_BYTES} bytes]"
    )


MCP_PREFIX = "mcp__"
_OUTPUT_MEDIA_TYPE = "application/json"

_ALIAS_RE = re.compile(r"^[a-z0-9_-]{1,32}$")
_NAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_UNSAFE_RE = re.compile(r"[^A-Za-z0-9_-]")


class McpConfigError(ValueError):
    """A fail-fast MCP configuration / discovery fault (bad alias, name
    collision, unmappable tool name). Raised at config-parse or
    ``prepare`` time — never swallowed into a ``ToolResult``."""


@dataclass(frozen=True, slots=True)
class McpServerSpec:
    """One operator-named local stdio MCP server. ``argv`` is the launch
    command (``argv[0]`` + args); never run through a shell.

    ``env`` carries extra environment variables for the spawned process
    (issue 02 — stdio configured from the front-end). It rides into
    the scrubbed env at spawn time only; it never enters any event/recording.

    ``tool_subset`` is the per-server **raw tool name** allow-list
    chosen by the user at config time: ``None`` ⇒ keep every advertised tool
    (back-compat); a tuple ⇒ keep only those whose raw ``tools/list`` name is in
    the set (others never enter the tool set / never reach the model). The subset
    lives host-side and never rides a request body (D3)."""

    alias: str
    argv: tuple[str, ...]
    env: tuple[tuple[str, str], ...] = ()
    tool_subset: Optional[tuple[str, ...]] = None

    def __post_init__(self) -> None:
        if not _ALIAS_RE.match(self.alias):
            raise McpConfigError(
                f"invalid MCP server alias {self.alias!r} "
                "(must match ^[a-z0-9_-]{1,32}$)"
            )
        if not self.argv or not self.argv[0]:
            raise McpConfigError(f"MCP server {self.alias!r} has an empty command")

    def env_dict(self) -> dict[str, str]:
        return {k: v for k, v in self.env}


@dataclass(frozen=True, slots=True)
class McpHttpServerSpec:
    """One remote HTTP MCP server.

    ``url`` is the single JSON-RPC endpoint; ``headers`` carry the static
    credential / custom headers (a Bearer token / API key) injected on every
    request (D5). **Credentials live here only** — passed from the host-side
    config store at build time; they ride on the wire and are NEVER written to
    any event, recording, or request body (D3). The discovered tools are wrapped
    as the same ``mcp__{alias}__{tool}`` ``McpTool``s as the stdio path, so
    naming / collision / R-1 resume-rebuild are shared verbatim.

    ``tool_subset``: same per-server raw-name allow-list as the
    stdio spec — ``None`` ⇒ keep all; a tuple ⇒ keep only those raw names."""

    alias: str
    url: str
    headers: tuple[tuple[str, str], ...] = ()
    tool_subset: Optional[tuple[str, ...]] = None

    def __post_init__(self) -> None:
        if not _ALIAS_RE.match(self.alias):
            raise McpConfigError(
                f"invalid MCP server alias {self.alias!r} "
                "(must match ^[a-z0-9_-]{1,32}$)"
            )
        if not self.url:
            raise McpConfigError(f"MCP server {self.alias!r} has an empty url")

    def headers_dict(self) -> dict[str, str]:
        return {k: v for k, v in self.headers}


#: A server spec the live build can connect: local stdio (``McpServerSpec``) or
#: remote HTTP (``McpHttpServerSpec``). Both map to the same ``McpTool`` set.
McpAnyServerSpec = Union[McpServerSpec, McpHttpServerSpec]


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
    """One enabled MCP server that ``build_mcp_tools`` could not connect (D7).

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
    (those live on the spec but are deliberately dropped here, D3) — so the
    record is safe to persist in any event / recording / task provenance. The actual
    tool shape / behaviour is NOT carried here; that is R-1's job (the recorded
    ``request_ref`` tool spec, rebuilt on resume). This is the audit answer to
    "what connectors + which of their tools was this task given this run".

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
    ``output`` is offloaded to a ContentStore artifact (reusing the
    shared inline byte budget)."""
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
        output = fit_output_fields(
            {"text": text, "text_ref": ref_json(ref), "non_text_blocks": non_text},
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


def _connect_one_server(
    spec: McpAnyServerSpec,
    *,
    spawn: Optional[SpawnFn],
    http_post: Optional[HttpPostFn],
) -> tuple[
    dict[str, McpTool], Union[McpStdioClient, McpHttpClient]
]:
    """Connect ONE server, discover + wrap its (subset-filtered) tools.

    Returns ``(built_tools_sorted, client)``. Any connect / handshake /
    ``tools/list`` / mapping / collision fault propagates (``McpError`` /
    ``McpConfigError``); on a raise the caller owns tearing down the partially
    started ``client`` (returned via the ``BaseException`` path is not possible,
    so this helper shuts its own client down before re-raising)."""
    client = _connect_client(spec, spawn=spawn, http_post=http_post)
    try:
        client.start()
        # per-server tool subset (the user-chosen allow-list, raw
        # ``tools/list`` names). ``None`` ⇒ keep all (back-compat); a tuple ⇒
        # drop any advertised tool not in it BEFORE it is wrapped, so unselected
        # tools never enter the tool set / reach the model. The surviving set is
        # sorted below, so order/stable-hash determinism (D7) is unchanged.
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
            raw_desc = raw.get("description")
            built[noeta_name] = McpTool(
                name=noeta_name,
                remote_tool_name=str(raw_name),
                input_schema=schema,
                client=client,
                description=raw_desc if isinstance(raw_desc, str) else "",
            )
    except BaseException:
        # The partial connection of THIS server is dead — drop it. (On the
        # skip-on-failure path the caller catches the re-raise and records the
        # skip; on the fail-fast path the batch caller tears down the rest.)
        client.shutdown()
        raise
    ordered = {noeta_name: built[noeta_name] for noeta_name in sorted(built)}
    return ordered, client


def build_mcp_tools(
    specs: tuple[McpAnyServerSpec, ...],
    *,
    spawn: Optional[SpawnFn] = None,
    http_post: Optional[HttpPostFn] = None,
    skip_on_failure: bool = False,
) -> tuple[
    dict[str, McpTool],
    list[Union[McpStdioClient, McpHttpClient]],
    list[McpServerSkip],
]:
    """Connect each server, discover its tools, and build the namespaced
    ``McpTool`` set. Specs may be local stdio (``McpServerSpec``) or remote
    HTTP (``McpHttpServerSpec``); both map to the same ``mcp__{alias}__{tool}``
    tools. **Deterministic order (D7)**: servers in ``specs``
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

    Returns ``(tools, clients, skipped)``; ``skipped`` is always ``[]`` when
    ``skip_on_failure=False``. Callers must ``shutdown`` the returned clients.
    Returns ``({}, [], [])`` for empty ``specs`` so the default-off path
    constructs nothing.

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
                    spec, spawn=spawn, http_post=http_post
                )
            except (McpError, McpConfigError) as exc:
                if not skip_on_failure:
                    raise
                # D7 option B: drop this server, record the skip, keep going.
                skipped.append(McpServerSkip(alias=spec.alias, reason=str(exc)))
                continue
            clients.append(client)
            tools.update(built)
    except BaseException:
        for c in clients:
            c.shutdown()
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
