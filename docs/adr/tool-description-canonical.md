# `Tool.description` is the canonical source of tool semantics; the prompt carries only role and cross-tool strategy

## Context

Tool semantics can travel through two channels: the structured tool schema the
provider API defines, or the system prompt's prose. Splitting them across both
gives one contract two sources of truth. Two constraints narrow the choice: the
tool set is dynamic (MCP servers, skill scripts and host-registered tools arrive
at build time, long after any preset prompt was written), and the rendered tool
schemas fold into the composer's stable-prefix hash, so whatever carries the
semantics also moves the prompt-cache key.

## Decision

**`description` is a field of the `Tool` protocol**, beside `name` and
`input_schema` — the same structured, LLM-facing group. `ContextComposer`
renders it into the provider function dict **only when non-empty**, so a tool
with no description produces byte-identical schema bytes. Each provider adapter
serializes it. The preset prompts carry no tool catalog.

**It is treated exactly like `input_schema`.** It stays out of `ToolRef`, whose
descriptor is `(name, version, risk_level)`. Authors pin a semantic change by
bumping the tool's `version`; the description itself folds into the
stable-prefix hash along with the rest of `provider_tool_schemas`, so editing it
rotates the prompt-cache key exactly as editing a schema does, and a resume that
rebuilds the tool set from the recorded request stays byte-identical with it.

**A first-party description is hand-written LLM-facing text, never a
docstring.** Both the `Tool` classes and the `@tool` decorator take an explicit
`description`; nothing auto-pulls `fn.__doc__`. An MCP tool's description comes
from the remote server, so `McpToolSpec` and `parse_mcp_tool_specs` carry it and
record it verbatim — a resume reconstructs the tool set from the first recorded
request without reconnecting, and reproduces the same text.

**The prompt keeps role plus cross-tool working strategy.** The dividing line:
what a tool *is* goes in its `description`; how this agent *works* across tools
(read → edit → verify → run tests; reason before calling) stays in the prompt.
The prompt prefers general phrasing over naming individual tools, and a narrow
trade-off between two tools is stated in the description of the tool it
concerns.

## Rationale

- **One contract, one channel.** Half a contract in a structured field and half
  in prose is two sources of truth that drift. The model is trained to read the
  `tools[].description` channel; leaving it empty forces the model to mine the
  semantics out of prose.
- **A static prompt cannot hold a dynamic tool set.** MCP, skill-script and
  host-registered tools bring their own descriptions and cannot be written into
  a preset prompt in advance. A canonical field carries them without welding
  "render into some prompt text" into the neutral layer.
- **Mirroring `input_schema` buys consistent handling for free.** The descriptor
  keeps carrying a reference rather than content, and the description joins the
  same stable-prefix cache key and the same resume-rebuild guarantee as the
  schema, with no second fingerprint rule to maintain.
- **Hand-written text is the only text safe to ship.** Docstrings are written
  for the next editor and carry internal shorthand; `input_schema` sets the
  hand-written, LLM-facing baseline for this group of fields.

## Alternatives considered

1. **Keeping tool semantics in the prompt as product-tunable prose.** Rejected:
   the composer would render half the contract while the other half lives
   elsewhere, and the dynamic tools could never be described at all.
2. **Putting `description` into `ToolRef`.** Rejected: it pours long text into a
   descriptor whose job is to carry a reference, and it would treat the
   description differently from `input_schema`.
3. **Auto-pulling `fn.__doc__`.** Rejected: it ships internal notes to the model
   and makes every docstring edit a silent prompt-cache rotation.
4. **Emitting the `description` key unconditionally.** Rejected: an empty string
   for an undocumented tool changes that tool's recorded schema bytes and busts
   the provider prompt cache for no gain.

## Consequences

- The field is in `noeta.protocols.tool`; the conditional render is in
  `noeta.context.composer`. The `@tool` decorator in `noeta.tools.decorator`
  exposes it as an explicit parameter.
- Built-in tool text ships as a resource beside each tool's implementation; MCP
  descriptions are captured and recorded in the mcp built-in's tool module.
- Editing a description moves the stable-prefix hash. Authors bump the tool's
  `version` alongside the edit so the descriptor reflects the change and resume
  rebuilding stays byte-identical.
