# Permission gating

Control which tool calls actually execute. Under the `default` permission
mode, high-risk built-in tools (`write`, `edit`, `apply_patch`, `shell_run`)
are *gated*: before one runs, Noeta calls your `can_use_tool` callback.
Return `True` to let it through, `False` to deny it.

This is the programmatic, in-process equivalent of a human clicking
"approve / deny" in the web UI.

## Example: deny all writes

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

## Permission modes

| Mode | Behaviour |
| --- | --- |
| `"default"` | High-risk tools are gated. `can_use_tool` decides; if absent or returns `None`, the call suspends for human approval. |
| `"acceptEdits"` | File edits (`edit`, `write`, `apply_patch`) are auto-approved; `shell_run` is still gated. |
| `"bypassPermissions"` | All tools run without approval. Use only in trusted, offline environments. |

## What the model sees

When a call is denied, the model receives a denial message in the next
View. It can then:

- Try a different approach (e.g. use `read` instead of `write`)
- Ask the user for clarification
- Accept the constraint and finish

The agent loop never breaks on a denial — it's just another observation
the model can react to.

## More sophisticated policies

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

## Key points

- **`can_use_tool` is wiring, not identity.** Two `Options` differing only
  in `can_use_tool` share the same compiled agent spec. This matches the
  treatment of `provider` and `cwd`.
- **The callback is synchronous.** Don't do heavy IO — return quickly.
  For async approval flows, use the web UI's approve/deny endpoints.
- **Denial ≠ failure.** The model sees the denial and can adapt. The
  task continues normally.

## Source

- `examples/permission_gate.py` — full runnable demo
- `Options.can_use_tool` — `packages/noeta-sdk/noeta/client/options.py`
- Permission modes: `_PERMISSION_MODES` in `packages/noeta-sdk/noeta/client/options.py`
- See also: [ADR: Guard-observer hooks](../adr/guard-observer-hooks.md),
  [ADR: Shell permission and background](../adr/shell-permission-and-background.md)
