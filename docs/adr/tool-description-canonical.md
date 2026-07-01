# Structured `description` is the canonical source of tool semantics; the prompt keeps only role and cross-tool working strategy

## Context

Tool semantics used to be split: half went through the structured channel (`name` + `input_schema`), while the other half (the semantics themselves) was scattered through the prose of the preset prompt — two sources of truth, which inevitably drift. The model is specifically trained to read the `tools[].description` channel, yet we left it empty and forced the model to dig the semantics out of prompt prose. This decision moves tool semantics from prompt prose into the structured `Tool.description`, and the prompt keeps only role and cross-tool working strategy.

Provider neutrality — and the point that "half a contract should not live somewhere else" — is covered in `provider-neutral.md`; the composer's rendering of the stable_prefix and the stable-prefix cache constraint are in the Stable Prefix entry of CONTEXT.md; keeping the ToolRef descriptor lean is covered in `agent-identity-and-provenance.md`.

## Decision

### `description` is a canonical field of `Tool`, rendered by the composer, serialized by each adapter, and never entering the prompt

Add `description` to the `Tool` protocol, alongside `name` / `input_schema` (they all belong to the same "structured contract, facing the LLM" group). `ContextComposer._render_provider_tool_schemas` emits `description` into the function dict **conditionally** (it only adds this key when non-empty, so a tool without a description keeps the same schema bytes). Each provider adapter is responsible for serializing it. The preset prompt no longer restates "what a tool is."

### Treat `description` like `input_schema`; don't invent a new fingerprint rule for it

It does not go into `ToolRef` (the descriptor stays lean: `(name, version, risk_level)`). The spec layer pins it by having authors bump the tool's `version`; the description, together with `provider_tool_schemas`, folds automatically into the composer's stable_prefix hash. So changing a description moves the stable-prefix hash (the prompt-cache key) just as changing a schema does, and when resume rebuilds the tool set from the recording it stays byte-identical with it.

### A first-party tool's `description` is a deliberately hand-written, LLM-facing string, not a docstring

Both the Tool class and the `@tool` decorator get a hand-written `description`. **It does not auto-pull `fn.__doc__`**: noeta's tool docstrings carry internal codenames (edit.py has things like `(B5)`, `Phase-4`), and auto-pulling would ship those straight to the model; `input_schema` already set the "hand-written, LLM-facing" baseline. A knock-on effect: an MCP tool's description comes from the remote server, so `parse_mcp_tool_specs` / `McpToolSpec` each carry a `description` field, **recorded verbatim**, so that resume rebuilding (reconstructing the tool set from the first recorded `LLMRequest.tools`, without reconnecting to the server) reproduces the same description — consistent with how input_schema is handled.

### The prompt keeps "role + cross-tool working strategy"; prefer general rules and use per-category phrasing sparingly

`MAIN_SYSTEM_PROMPT` drops the `Tools:` enumeration; it keeps the role sentence plus `Rules` (read→edit→verify with git_diff→run tests; reason before calling). The dividing line: the **tool catalog/contract** (what a tool is) goes into `description`; the **cross-tool working strategy** (how this agent works) stays in the prompt. Prefer general rules and use per-category phrasing sparingly ("use the search tool to locate the exact line" rather than naming `grep`, consistent with Claude Code's file/search vs shell phrasing); and sink **narrow paired trade-offs** (replace_text vs apply_patch) into `apply_patch.description`, not the prompt. The set of four (MAIN/GENERAL_PURPOSE/EXPLORE/PLAN) is treated identically.

## Rationale

- **A single contract should not be split in two.** Before, half the contract (`name` + `input_schema`) went through the structured channel and the other half (the semantics) went through prompt prose — two sources of truth that inevitably drift. The model is specifically trained to read the `tools[].description` channel, yet we left it empty and forced the model to dig the semantics out of prompt prose. noeta was the outlier here (Claude Code's own system prompt has no "Tools:" catalog anywhere; per-tool semantics live 100% in the structured layer).
- **This asymmetry should be eliminated.** Both Agent (description in spawn_subagent's schema) and Skill (menu in the skill's schema) feed a description into some tool schema; only Tool — the thing actually callable — had no description of its own.
- **A prose prompt cannot hold dynamic tools.** MCP / skill-script / user-registered tools carry their own descriptions and cannot be written into a static preset prompt ahead of time. A canonical field can hold them while preserving provider neutrality (not welding "render into some prompt text" into the canonical layer).
- **Mirroring input_schema and staying out of ToolRef gives both consistent handling and a descriptor that carries only a ref.** Putting it in `ToolRef` would pour long text into the descriptor (violating "descriptors carry a ref, not content") and treat it differently from input_schema; folding it into the composer hash alongside the schema makes it, for free, part of the same stable-prefix cache key (consistent with resume rebuilding).
- **Hand-written beats docstring, because docstrings carry internal codenames that would be shipped to the model**, whereas input_schema already set the hand-written, LLM-facing baseline.

## Alternatives considered

1. **Keep tool semantics in the prompt as "product-tunable prose."** Rejected: that has the kernel composer render only half the contract while the semantics live elsewhere — exactly today's inconsistency.
2. **Put `description` into `ToolRef`.** Rejected: it pours long text into the descriptor, violates "descriptors carry only a ref," and treats it differently from input_schema.
3. **Auto-pull `fn.__doc__` as the description.** Rejected: docstrings carry internal codenames that would be shipped to the model; input_schema already set the hand-written baseline.

## Consequences

- Landing points: `noeta.protocols.tool` (the `Tool.description` field), `noeta.context.composer` (`_render_provider_tool_schemas` emits description conditionally).
- Hand-written text lands in `noeta.tools.decorator` (`@tool`'s description parameter/slot) and `noeta.tools.descriptions` / `noeta.policies.descriptions` (hand-written, LLM-facing description text).
- MCP tool descriptions are recorded verbatim in `noeta.tools.mcp.tool` (`McpToolSpec` / `parse_mcp_tool_specs`), reproduced by resume rebuilding.
- The set-of-four preset slimming lands in `noeta.presets` (apply_patch.description absorbs replace_text's trade-off hint).
- Note: changing a description moves the stable-prefix hash, so authors pin it by bumping the tool's `version`, guaranteeing byte-identical resume rebuilding.
