# 连接 MCP 服务器

**目标：** 注册远程（stdio 或 HTTP）MCP 服务器，让 Noeta 可以使用它们的工具，或将你自己的工具打包为进程内 MCP 服务器。

**开始之前：** 你有一个可用的 Noeta 安装。你知道 MCP（Model Context Protocol）是什么，并且有一个想要连接的 MCP 服务器。

## 方案 A：通过 coding agent 使用远程 MCP

对于 `python -m noeta.agent`，MCP 服务器注册在主机的连接器存储中，位于 `~/.noeta/mcp_servers.json`：

```json
{
  "servers": {
    "github": {
      "type": "http",
      "url": "https://mcp.github.com/mcp",
      "headers": {
        "Authorization": "Bearer ghp_…"
      }
    },
    "filesystem": {
      "type": "stdio",
      "command": "npx",
      "args": ["-y", "@modelcontextprotocol/server-filesystem", "/path/to/dir"],
      "env": {}
    }
  }
}
```

每个条目有一个 `alias`（键名——`github`、`filesystem`），代理用它来引用服务器。`type` 为 `"http"` 或 `"stdio"`。

凭据（header 值、环境变量）存储在主机端，**永远不会**出现在请求体中或 `/mcp/servers` 发现响应中（该响应返回已清除凭据的条目）。

### 按会话启用 MCP

已注册的服务器不会自动使用。你在创建任务时通过 `enabled_mcp` 字段按会话启用它们：

```bash
# 通过 HTTP
curl -X POST http://127.0.0.1:<port>/tasks \
  -H "Content-Type: application/json" \
  -d '{"goal": "Read the repo README", "enabled_mcp": ["github"]}'
```

或在 Web 界面中，创建新会话时从下拉菜单中选择 MCP 服务器。

启用后，MCP 服务器的工具在代理的工具允许列表中显示为 `mcp__<alias>__<tool_name>`。

### 通过 HTTP 管理服务器

后端为连接器存储暴露了 CRUD 端点：

| 路由 | 功能 |
| --- | --- |
| `GET /mcp/servers` | 列出已注册服务器（已清除凭据） |
| `POST /mcp/servers` | 注册新服务器 |
| `PUT /mcp/servers/{alias}` | 合并编辑现有服务器 |
| `DELETE /mcp/servers/{alias}` | 移除服务器 |
| `GET /mcp/servers/{alias}/tools` | 发现服务器的工具菜单 |
| `GET /mcp/servers/{alias}/prompts` | 发现服务器的 prompts |
| `GET /mcp/servers/{alias}/resources` | 发现服务器的资源 |

完整请求和响应格式参见 [HTTP 接口参考](../reference/http-api.md)。

## 方案 B：进程内 SDK MCP 服务器

对于希望将自己的工具打包成 MCP 形态服务器的 SDK 用户，使用 `create_sdk_mcp_server`：

```python
from noeta.sdk import create_sdk_mcp_server, tool
from noeta.protocols.tool import ToolContext, ToolResult

@tool(name="echo", version="1", risk_level="low",
      input_schema={"type": "object", "properties":
                    {"text": {"type": "string"}}, "required": ["text"]})
def echo(arguments: dict, ctx: ToolContext) -> ToolResult:
    return ToolResult(success=True, output=arguments["text"])

my_mcp = create_sdk_mcp_server(
    name="my-tools",
    version="1.0.0",
    tools=(echo,),
)
```

在 `Options` 中挂载：

```python
from noeta.sdk import Options

options = Options(
    system_prompt="...",
    name="my-agent",
    mcp_servers=(my_mcp,),
)
```

工具显示为 `mcp__my-tools__echo`——与远程 MCP 服务器使用相同的命名约定，但它们在进程内运行，无子进程或网络往返。

## 验证连接

注册服务器后，验证工具发现是否正常工作：

```bash
curl http://127.0.0.1:<port>/mcp/servers/github/tools
```

你应该看到服务器的工具菜单作为 JSON 数组返回。如果收到 502，说明服务器已注册但连接或握手失败——检查 URL、headers，并确认 MCP 服务器确实在运行。

## 另请参阅

- [构建自定义工具](build-custom-tools.md) — 用 `@tool` 定义工具并将它们打包为 SDK MCP 服务器
- [Coding agent 参考](../reference/noeta-agent.md) — MCP 的环境配置
- [HTTP 接口参考](../reference/http-api.md) — MCP 路由详情
- `examples/mcp_server.py` — 完整进程内 MCP 示例
