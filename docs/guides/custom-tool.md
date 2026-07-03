# Custom tools

Give your agent a custom tool with the `@tool` decorator. A tool is just a
function `fn(arguments: dict, ctx: ToolContext) -> ToolResult` — wrap it,
drop it into `Options.allowed_tools`, and the SDK wires the live closure
into the session while its identity ref enters the agent's declared spec.

## Example: a word-count tool

```python
import tempfile
from pathlib import Path

from noeta.protocols.messages import (
    LLMResponse,
    TextBlock,
    ToolUseBlock,
    Usage,
)
from noeta.protocols.tool import ToolContext, ToolResult
from noeta.sdk import Options, query, tool
from noeta.testing.fake_llm import FakeLLMProvider

# --- 1. Define the JSON Schema for your tool's input -----------------------

_WORD_COUNT_SCHEMA = {
    "type": "object",
    "properties": {"text": {"type": "string"}},
    "required": ["text"],
    "additionalProperties": False,
}

# --- 2. Decorate a plain function ------------------------------------------
#
# @tool takes:
#   name          — the string the model calls (snake_case, provider-safe)
#   version       — part of the tool's declared identity (required, no default)
#   risk_level    — "low" (always allowed) or "high" (subject to permission gate)
#   input_schema  — JSON Schema dict for the arguments dict
#
# The function signature must be:
#   def my_tool(arguments: dict, ctx: ToolContext) -> ToolResult:
#
# Return ToolResult(success=True, output="...") on success,
# ToolResult(success=False, output="error message") on failure.

@tool(
    name="word_count",
    version="1",
    risk_level="low",
    input_schema=_WORD_COUNT_SCHEMA,
)
def word_count(arguments: dict, ctx: ToolContext) -> ToolResult:
    """Count whitespace-separated words in the input text."""
    n = len(str(arguments.get("text", "")).split())
    return ToolResult(success=True, output=f"{n} words")

# --- 3. Mount it on Options ------------------------------------------------
#
# Pass the decorated closure by value — that's how a custom tool gets both
# wired (runnable) and identified (its ref). You can mix name strings and
# @tool instances in the same tuple.

options = Options(
    system_prompt="You count words when asked.",
    name="counter",
    allowed_tools=(word_count,),
    permission_mode="bypassPermissions",
)

# --- 4. Run -----------------------------------------------------------------

# Scripted provider: call word_count once, then finish.
provider = FakeLLMProvider(
    responses=[
        LLMResponse(
            stop_reason="tool_use",
            content=[
                ToolUseBlock(
                    call_id="wc-1",
                    tool_name="word_count",
                    arguments={"text": "the quick brown fox"},
                )
            ],
            usage=Usage(uncached=1, output=1),
        ),
        LLMResponse(
            stop_reason="end_turn",
            content=[TextBlock(text="Counted the words.")],
            usage=Usage(uncached=1, output=1),
        ),
    ]
)

with tempfile.TemporaryDirectory(prefix="noeta-customtool-") as tmp:
    envelopes = query(
        options,
        goal="How many words are in 'the quick brown fox'?",
        provider=provider,
        workspace_dir=Path(tmp),
        model="stub-model",
    )

    called = [
        e.payload.tool_name
        for e in envelopes
        if e.type == "ToolCallStarted"
    ]
    print(f"tools the agent called: {called}")
    # → tools the agent called: ['word_count']
```

## Key points

- **`version` is mandatory.** Two behaviourally different tools must never
  share the same `name` + `version`. Bump it when semantics change.
- **`risk_level="high"`** gates the tool through `PermissionGuard`. The
  model can still *call* it, but the call suspends for approval unless
  `permission_mode="bypassPermissions"` or a `can_use_tool` callback
  approves it. See [Permission gating](permission-gating.md).
- **`input_schema`** is the contract the model sees. Keep it tight —
  `additionalProperties: false` prevents the model from sending junk.
- **Mix freely.** `allowed_tools=("read", "grep", my_custom_tool)` works:
  strings reference built-ins, `@tool` instances reference custom closures.

## Source

- `examples/custom_tool.py` — full runnable demo
- `noeta.sdk.tool` / `noeta.sdk.DecoratedTool` — `packages/noeta-sdk/noeta/sdk/authoring.py`
- `noeta.protocols.tool.ToolContext` / `ToolResult` — `packages/noeta-runtime/noeta/protocols/tool.py`
