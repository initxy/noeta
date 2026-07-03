# 自定义工具 { #custom-tools }

用 `@tool` 装饰器给你的代理一个自定义工具。工具就是一个函数 `fn(arguments: dict, ctx: ToolContext) -> ToolResult`——包装它，放入 `Options.allowed_tools`，SDK 会将实时闭包接入会话，同时其身份引用进入代理的声明 spec。

## 示例：词数统计工具 { #example-a-word-count-tool }

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

## 要点 { #key-points }

- **`version` 是必需的。** 两个行为不同的工具绝不能共享相同的 `name` + `version`。语义变化时请递增它。
- **`risk_level="high"`** 通过 `PermissionGuard` 门控该工具。模型仍然可以*调用*它，但调用会挂起等待批准，除非 `permission_mode="bypassPermissions"` 或 `can_use_tool` 回调批准了它。见[权限门控](permission-gating.md)。
- **`input_schema`** 是模型看到的契约。保持紧凑——`additionalProperties: false` 防止模型发送垃圾数据。
- **自由混合。** `allowed_tools=("read", "grep", my_custom_tool)` 有效：字符串引用内置工具，`@tool` 实例引用自定义闭包。

## 来源 { #source }

- `examples/custom_tool.py` —— 完整可运行演示
- `noeta.sdk.tool` / `noeta.sdk.DecoratedTool` —— `packages/noeta-sdk/noeta/sdk/authoring.py`
- `noeta.protocols.tool.ToolContext` / `ToolResult` —— `packages/noeta-runtime/noeta/protocols/tool.py`
