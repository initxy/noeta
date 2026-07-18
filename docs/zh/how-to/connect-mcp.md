# 连接 MCP 服务器

**目标：** 让空间（space）的 agent 能访问 MCP（Model Context Protocol）服务器，或把你自己的工具打包成进程内 MCP 服务器，供 SDK agent 使用。

**开始之前：** 你有一个正在运行的平台（或一套可用的 SDK 环境），以及一个想接入的 MCP 服务器。

## 方案 A：平台上的每空间连接器

MCP 连接器是**空间作用域**的配置，在空间的 MCP 页面（或通过 API）管理。不存在全局注册表文件 —— 已退役的 `~/.noeta/mcp_servers.json` 机制不复存在；每个空间在应用数据库里维护自己的一套连接器。

以别名（alias）注册连接器，传输方式二选一：

- **`http`** —— 一个 URL 加可选的 headers（bearer token 等）。
- **`stdio`** —— 本地服务器进程的 command、args 和 env。

然后，对每个连接器可以：

- **启用 / 禁用。** 启用的连接器**每一轮**都会解析进 agent host —— 无需重启会话（session）；其工具对模型显示为 `mcp__<alias>__<tool>`。
- **限制工具子集。** 先发现服务器的完整工具菜单，再只保留想暴露的工具（`null` = 全部）。

凭证（header 值、env 值）存储在服务端，**绝不**回显 —— 列出连接器时返回的是抹去凭证后的条目。只有空间所有者能管理连接器；成员可以查看。

通过 HTTP（全部位于 `/api/v1/spaces/{space_id}/mcp` 下）：

| 路由 | 作用 |
| --- | --- |
| `GET /servers` | 列出连接器（凭证已抹去） |
| `POST /servers` | 注册连接器（`alias`、`type`、传输字段、可选的 `tools` 子集） |
| `PUT /servers/{alias}` | 合并式编辑现有连接器 |
| `PATCH /servers/{alias}` | 启用 / 禁用 |
| `DELETE /servers/{alias}` | 移除 |
| `GET /servers/{alias}/tools` | 发现工具菜单 |
| `PUT /servers/{alias}/tools` | 设置启用的工具子集（`null` = 全部） |
| `GET /servers/{alias}/prompts` · `/resources` | 发现 prompts / resources |

发现仅走 HTTP：对 `stdio` 连接器发起发现会得到 400（服务器不会因为一次管理性的 GET 就去启动运维者配置的子进程）；连接或握手失败会得到 502 —— 检查 URL、headers，并确认 MCP 服务器确实在运行。

## 方案 B：进程内 SDK MCP 服务器

想把自己的工具打包成 MCP 形态服务器的 SDK 用户，可以用 `create_sdk_mcp_server`：

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

这些工具显示为 `mcp__my-tools__echo` —— 与远程 MCP 服务器同一套命名约定，但它们在进程内运行，没有子进程，也没有网络往返。

## 另请参阅

- [构建自定义工具](build-custom-tools.md) —— 用 `@tool` 定义工具并打包进 SDK MCP 服务器
- [HTTP API 参考](../reference/http-api.md#mcp-connectors) —— 请求与响应的形态
- `examples/mcp_server.py` —— 完整的进程内 MCP 示例
