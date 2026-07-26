# 连接 MCP 服务器

**目标：** 让你的 SDK agent 能访问 MCP（Model Context Protocol）服务器 ——
外部的 `stdio` / `http` 服务器，或把你自己的工具打包成进程内服务器。

**开始之前：** 一套可用的 `noeta.sdk` 环境，以及一个想接入的 MCP 服务器。

MCP 服务器通过 `Options.mcp_servers` 挂载。其工具对模型显示为
`mcp__<alias>__<tool>`；每个服务器的 `tool_subset` 只保留你想暴露的工具
（`None` = 全部）。

## 外部服务器（`stdio` / `http`）

`noeta.sdk` 导出两种服务器 spec —— 一个子进程（`stdio`）服务器，一个远程
（`http`）服务器：

```python
from noeta.sdk import Options, McpServerSpec, McpHttpServerSpec

stdio = McpServerSpec(
    alias="fs",
    argv=("npx", "-y", "@modelcontextprotocol/server-filesystem", "/data"),
    tool_subset=("read_file", "list_directory"),   # None = 全部工具
)
http = McpHttpServerSpec(
    alias="search",
    url="https://mcp.example.com/sse",
    headers=(("Authorization", "Bearer …"),),
)

options = Options(
    system_prompt="…",
    name="my-agent",
    mcp_servers=(stdio, http),
)
```

凭证（headers、env）只存在于你构造的 spec 里 —— 绝不会写进记录，也绝不进入
模型可见的工具 schema。

## 进程内 SDK MCP 服务器

想把自己的工具打包成 MCP 形态服务器，用 `create_sdk_mcp_server`：

```python
from noeta.sdk import create_sdk_mcp_server, tool, ToolContext, ToolResult

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

以同样方式挂载：

```python
from noeta.sdk import Options

options = Options(
    system_prompt="...",
    name="my-agent",
    mcp_servers=(my_mcp,),
)
```

这些工具显示为 `mcp__my-tools__echo` —— 与外部 MCP 服务器同一套命名约定，但
它们在进程内运行，没有子进程，也没有网络往返。

## 另请参阅

- [构建自定义工具](build-custom-tools.md) —— 用 `@tool` 定义工具并打包进 SDK MCP 服务器
- [ADR：MCP connectors](https://github.com/initxy/noeta/blob/main/docs/adr/mcp-connectors.md) —— 每连接器工具子集背后的设计
- `examples/mcp_server.py` —— 完整的进程内 MCP 示例
