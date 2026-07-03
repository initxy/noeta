# MCP 连接器 { #mcp-connectors }

Noeta 以两种方式支持 [Model Context Protocol](https://modelcontextprotocol.io/)（MCP）：

1. **进程内 MCP servers** —— 通过 `create_sdk_mcp_server()` 将 `@tool` 函数打包到一个命名的进程内 server 中。工具在宿主进程中运行（无子进程，无网络）。
2. **外部 MCP servers** —— 连接到在主机配置中注册的远程 HTTP 或本地 stdio MCP servers。工具显示为 `mcp__<alias>__<tool>`。

## 进程内 MCP server { #in-process-mcp-server }

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
from noeta.sdk import Options, create_sdk_mcp_server, query, tool
from noeta.testing.fake_llm import FakeLLMProvider

# --- 1. Define tools with @tool --------------------------------------------

_TEXT_SCHEMA = {
    "type": "object",
    "properties": {"text": {"type": "string"}},
    "required": ["text"],
    "additionalProperties": False,
}

@tool(name="echo", version="1", risk_level="low", input_schema=_TEXT_SCHEMA)
def echo(arguments: dict, ctx: ToolContext) -> ToolResult:
    """Return the input text unchanged."""
    return ToolResult(success=True, output=str(arguments.get("text", "")))

@tool(name="shout", version="1", risk_level="low", input_schema=_TEXT_SCHEMA)
def shout(arguments: dict, ctx: ToolContext) -> ToolResult:
    """Return the input text upper-cased."""
    return ToolResult(success=True, output=str(arguments.get("text", "")).upper())

# --- 2. Bundle into a named MCP server -------------------------------------
#
# create_sdk_mcp_server returns a frozen SdkMcpServer value object.
# Hand it to Options.mcp_servers — its tools become available by name.

toolbox = create_sdk_mcp_server("toolbox", version="1.0.0", tools=[echo, shout])

# --- 3. Mount on Options ---------------------------------------------------

options = Options(
    system_prompt="You echo or shout text when asked.",
    name="toolbox-user",
    mcp_servers=(toolbox,),
    permission_mode="bypassPermissions",
)

# --- 4. Run -----------------------------------------------------------------

provider = FakeLLMProvider(
    responses=[
        LLMResponse(
            stop_reason="tool_use",
            content=[
                ToolUseBlock(
                    call_id="e-1",
                    tool_name="echo",
                    arguments={"text": "hello from the toolbox"},
                )
            ],
            usage=Usage(uncached=1, output=1),
        ),
        LLMResponse(
            stop_reason="end_turn",
            content=[TextBlock(text="Echoed it.")],
            usage=Usage(uncached=1, output=1),
        ),
    ]
)

with tempfile.TemporaryDirectory(prefix="noeta-mcp-") as tmp:
    envelopes = query(
        options,
        goal="Echo 'hello from the toolbox'.",
        provider=provider,
        workspace_dir=Path(tmp),
        model="stub-model",
    )

    called = [
        e.payload.tool_name
        for e in envelopes
        if e.type == "ToolCallStarted"
    ]
    print(f"tools called from MCP server: {called}")
    # → tools called from MCP server: ['echo']
```

### 何时使用进程内 vs. `allowed_tools` { #when-to-use-in-process-vs-allowed_tools }

`allowed_tools` 一次挂载一个松散的工具。`create_sdk_mcp_server` 将几个相关工具分组在一个 server 值对象下，因此整个工具箱作为一个单元旅行（和被标识）。在以下情况下使用进程内 MCP：

- 你有一组内聚的工具，它们属于一起
- 你想要该包的版本化身份
- 你希望工具可通过 MCP 工具列表 API 发现

## 外部 MCP servers（应用模式） { #external-mcp-servers-app-mode }

运行完整的 `python -m noeta.agent` 应用时，外部 MCP servers 通过主机配置（JSON 文件或 HTTP API）注册。

### 通过配置文件注册 { #register-via-config-file }

添加到你的 `NOETA_AGENT_CONFIG` JSON：

```json
{
  "mcp_servers": {
    "github": {
      "type": "http",
      "url": "https://mcp.github.com/mcp",
      "headers": {
        "Authorization": "Bearer <your-token>"
      }
    },
    "filesystem": {
      "type": "stdio",
      "command": "npx",
      "args": ["-y", "@modelcontextprotocol/server-filesystem", "/path/to/dir"]
    }
  }
}
```

### 通过 HTTP API 注册 { #register-via-http-api }

代理运行时，你可以通过 HTTP API 管理 MCP servers：

```bash
# List registered servers
curl http://127.0.0.1:8765/mcp/servers

# Register an HTTP MCP server
curl -X POST http://127.0.0.1:8765/mcp/servers \
  -H "Content-Type: application/json" \
  -d '{
    "alias": "github",
    "type": "http",
    "url": "https://mcp.github.com/mcp",
    "headers": {"Authorization": "Bearer <token>"}
  }'

# Register a stdio MCP server
curl -X POST http://127.0.0.1:8765/mcp/servers \
  -H "Content-Type: application/json" \
  -d '{
    "alias": "fs",
    "type": "stdio",
    "command": "npx",
    "args": ["-y", "@modelcontextprotocol/server-filesystem", "/tmp"]
  }'

# List tools from a server
curl http://127.0.0.1:8765/mcp/servers/github/tools

# Remove a server
curl -X DELETE http://127.0.0.1:8765/mcp/servers/github
```

### 按会话启用 MCP servers { #enabling-mcp-servers-per-session }

通过 `POST /tasks` 创建任务时，传递 `enabled_mcp` 字段以选择哪些已注册的 servers 可用于该会话：

```bash
curl -X POST http://127.0.0.1:8765/tasks \
  -H "Content-Type: application/json" \
  -d '{
    "goal": "Search the codebase for TODOs.",
    "agent": "main",
    "enabled_mcp": ["github", "fs"]
  }'
```

已启用的 MCP 工具在代理的工具集中显示为 `mcp__<alias>__<tool>`。

## 要点 { #key-points }

- **进程内 = `create_sdk_mcp_server()` + `Options.mcp_servers`。** 工具在本地运行，无网络。适合打包你自己的工具包。
- **外部 = 主机配置 + `/mcp/servers` API。** 连接远程 HTTP 或本地 stdio MCP servers。适合第三方 MCP 生态系统。
- **凭据永远不在请求 body 中传递。** 它们存储在主机端，并从 API 响应中清除。
- **`Options` 上的 `mcp_servers` 仅用于进程内。** 外部 servers 通过主机配置 / API 管理，而非配方。

## 来源 { #source }

- `examples/mcp_server.py` —— 完整的进程内 MCP 演示
- `noeta.sdk.create_sdk_mcp_server` —— `packages/noeta-sdk/noeta/sdk/authoring.py`
- MCP 路由处理器：`apps/noeta-agent/noeta/agent/backend/mcp_service.py`
- MCP 注册表：`apps/noeta-agent/noeta/agent/host/mcp_registry.py`
- 另见：[工具参考](../reference/tools.md)、[HTTP API](../reference/http-api.md#mcp-server-management)、[ADR：MCP 连接器](../adr/mcp-connectors.md)
