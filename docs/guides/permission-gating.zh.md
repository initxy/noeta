# 权限门控 { #permission-gating }

控制哪些工具调用实际执行。在 `default` 权限模式下，高风险内置工具（`write`、`edit`、`apply_patch`、`shell_run`）被*门控*：在其中一个运行之前，Noeta 调用你的 `can_use_tool` 回调。返回 `True` 放行，`False` 拒绝。

这是人类在 Web UI 中点击"批准/拒绝"的编程化、进程内等效物。

## 示例：拒绝所有写入 { #example-deny-all-writes }

```python
import tempfile
from pathlib import Path

from noeta.protocols.events import ToolCallApprovalResolvedPayload
from noeta.protocols.messages import (
    LLMResponse,
    TextBlock,
    ToolUseBlock,
    Usage,
)
from noeta.sdk import Options, query
from noeta.testing.fake_llm import FakeLLMProvider

# --- 1. Define your policy callback ----------------------------------------
#
# Signature: (tool_name: str, arguments: dict) -> bool
#   True  → allow the call
#   False → deny it (the model sees the denial and can react)

def deny_writes(tool_name: str, arguments: dict) -> bool:
    """Block every write, allow everything else."""
    return tool_name != "write"

# --- 2. Mount it on Options ------------------------------------------------
#
# permission_mode="default" enables the gate for high-risk tools.
# can_use_tool is your auto-approve/deny hook.

options = Options(
    system_prompt="You may write files when asked.",
    name="gated",
    allowed_tools=("write",),
    permission_mode="default",
    can_use_tool=deny_writes,
)

# --- 3. Run -----------------------------------------------------------------

# Scripted: attempt a write, then finish after denial.
provider = FakeLLMProvider(
    responses=[
        LLMResponse(
            stop_reason="tool_use",
            content=[
                ToolUseBlock(
                    call_id="w1",
                    tool_name="write",
                    arguments={"path": "secret.txt", "content": "oops\n"},
                )
            ],
            usage=Usage(uncached=1, output=1),
        ),
        LLMResponse(
            stop_reason="end_turn",
            content=[TextBlock(text="Understood — I won't write that file.")],
            usage=Usage(uncached=1, output=1),
        ),
    ]
)

with tempfile.TemporaryDirectory(prefix="noeta-perm-") as tmp:
    envelopes = query(
        options,
        goal="Write 'oops' to secret.txt.",
        provider=provider,
        workspace_dir=Path(tmp),
        model="stub-model",
    )

    # Inspect the approval resolution.
    resolved = [
        e.payload
        for e in envelopes
        if e.type == "ToolCallApprovalResolved"
        and isinstance(e.payload, ToolCallApprovalResolvedPayload)
    ]
    wrote_file = any(e.type == "ToolResultRecorded" for e in envelopes)

    if resolved:
        print(f"approved={resolved[0].approved}")
        print(f"resolver={resolved[0].resolver}")
    print(f"file written={wrote_file}")
    # → approved=False
    # → resolver=can_use_tool
    # → file written=False
```

## 权限模式 { #permission-modes }

| 模式 | 行为 |
| --- | --- |
| `"default"` | 高风险工具被门控。`can_use_tool` 决定；如果缺失或返回 `None`，调用挂起等待人类批准。 |
| `"acceptEdits"` | 文件编辑（`edit`、`write`、`apply_patch`）自动批准；`shell_run` 仍被门控。 |
| `"bypassPermissions"` | 所有工具无需批准即运行。仅在受信任的离线环境中使用。 |

## 模型看到什么 { #what-the-model-sees }

当调用被拒绝时，模型在下一个 View 中收到拒绝消息。然后它可以：

- 尝试不同的方法（例如使用 `read` 而非 `write`）
- 向用户请求澄清
- 接受约束并完成

代理循环不会因拒绝而中断——这只是模型可以做出反应的另一个观察结果。

## 更复杂的策略 { #more-sophisticated-policies }

```python
def allowlist_policy(tool_name: str, arguments: dict) -> bool:
    """Allow only reads and safe shell commands."""
    if tool_name in ("read", "glob", "grep", "webfetch"):
        return True
    if tool_name == "shell_run":
        cmd = str(arguments.get("command", ""))
        # Only allow git operations
        return cmd.startswith("git ")
    return False

def path_aware_policy(tool_name: str, arguments: dict) -> bool:
    """Block writes outside the docs directory."""
    if tool_name == "write":
        path = str(arguments.get("path", ""))
        return path.startswith("docs/")
    return True
```

## 要点 { #key-points }

- **`can_use_tool` 是接线，不是身份。** 两个仅在 `can_use_tool` 上不同的 `Options` 共享相同的编译代理 spec。这与 `provider` 和 `cwd` 的处理方式一致。
- **回调是同步的。** 不要做重型 IO——快速返回。对于异步批准流程，使用 Web UI 的 approve/deny 端点。
- **拒绝 ≠ 失败。** 模型看到拒绝并可以适应。任务正常继续。

## 来源 { #source }

- `examples/permission_gate.py` —— 完整可运行演示
- `Options.can_use_tool` —— `packages/noeta-sdk/noeta/client/options.py`
- 权限模式：`packages/noeta-sdk/noeta/client/options.py` 中的 `_PERMISSION_MODES`
- 另见：[ADR：Guard-Observer 钩子](../adr/guard-observer-hooks.md)、[ADR：Shell 权限与后台](../adr/shell-permission-and-background.md)
