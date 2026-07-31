# 连接 MCP 服务器

本指南教你让一个 agent 能访问 MCP（Model Context Protocol）服务器 —— 外部的 `stdio` / `http` 服务器，或把你自己的工具打包成的进程内服务器。你需要一套可用的 `noeta.sdk` 环境，以及一个想接入的服务器。

先挑对机制；它们不可互换：

| | 外部服务器 | 进程内服务器 |
| --- | --- | --- |
| 挂载于 | `HostConfig.mcp_server_resolver` | `Options.mcp_servers` |
| 启用方式 | 按回合，按 alias | 始终启用，对该 agent 生效 |
| 工具名 | `mcp__{alias}__{tool}` | 裸的 `@tool` 名字 |
| 运行于 | 子进程或经 HTTP | 你的进程 |

## 方案 A —— 外部服务器（`stdio` / `http`）

host 从不把配置存储交给 SDK。它提供一个**解析器（resolver）** —— `alias -> spec | None` —— 并在每个回合上指名要连接哪些 alias。这样凭证就留在 host 一侧：它们只存在于你构造的 spec 里，永远不会进入记录，也不会进入模型可见的工具 schema。

```python
from noeta.sdk import Client, HostConfig, McpHttpServerSpec, McpServerSpec, Options

SERVERS = {
    "fs": McpServerSpec(
        alias="fs",
        argv=("npx", "-y", "@modelcontextprotocol/server-filesystem", "/data"),
        tool_subset=("read_file", "list_directory"),   # None = every tool
    ),
    "search": McpHttpServerSpec(
        alias="search",
        url="https://mcp.example.com/rpc",
        headers=(("Authorization", "Bearer …"),),
    ),
}

client = Client(
    Options(system_prompt="…", name="my-agent"),
    provider=my_provider,
    host_config=HostConfig(mcp_server_resolver=SERVERS.get),
)

outcome = client.start(goal="list the data directory", enabled_mcp=("fs",))
```

启用 `fs` 这个 alias 之后，子集里的两个工具会以带命名空间的名字到达模型：

```
mcp__fs__read_file
mcp__fs__list_directory
```

`enabled_mcp` 是一个按回合、非持久的开关。每个驱动回合的动词都接受它（`start`、`send_goal`、`seed_start`、`seed_send_goal`），所以一场对话可以在某一回合启用某个服务器，下一回合不启用。`query` 没有 `enabled_mcp` 参数 —— 外部 MCP 需要一个 `Client`。

两种 spec 都是冻结的 dataclass，在构造时校验：

- `alias` 必须匹配 `^[a-z0-9_-]{1,32}$`。
- `McpServerSpec` 需要一个非空的 `argv`；它被直接执行，绝不经过 shell。`env` 为被派生的进程添加环境变量。
- `McpHttpServerSpec` 需要一个非空的 `url` —— 一个 JSON-RPC 端点。客户端用标准库 POST 一个请求并读取一个响应；如果你需要自己的传输，`HostConfig.mcp_http_post` 会替换掉它。
- `tool_subset` 按服务器的**原始**工具名过滤。`None` 保留每个被广告出来的工具；传一个元组则只保留列出的那些，其余的永远不会进入工具集。

每个被发现的工具都会变成一个名为 `mcp__{alias}__{tool}` 的普通 Tool，其中 `[A-Za-z0-9_-]` 之外的字符会被替换。整个名字必须匹配 `^[A-Za-z0-9_-]{1,64}$`；空的、过长的或撞车的名字会快速失败，而不是被悄悄截断。因为它们是普通 Tool，所以它们会走过 composer schema、Policy 和权限 Guard，没有任何特殊处理。

如果某个服务器在回合开始时连接失败，它会被丢弃，回合的其余部分照常进行；而重复的 alias 永远是一个硬性的 `McpConfigError`。

## 方案 B —— 进程内 SDK MCP 服务器

想把自己的工具打包成 MCP 形态的服务器，用 `create_sdk_mcp_server`：

```python
from noeta.sdk import Options, ToolContext, ToolResult, create_sdk_mcp_server, tool

@tool(
    name="echo",
    version="1",
    risk_level="low",
    description="Return the given text unchanged.",
    input_schema={
        "type": "object",
        "properties": {"text": {"type": "string"}},
        "required": ["text"],
        "additionalProperties": False,
    },
)
def echo(arguments: dict, ctx: ToolContext) -> ToolResult:
    return ToolResult(success=True, output=arguments["text"])

my_mcp = create_sdk_mcp_server(name="my-tools", version="1.0.0", tools=(echo,))

options = Options(
    system_prompt="...",
    name="my-agent",
    mcp_servers=(my_mcp,),
)
```

`Options.mcp_servers` 只接受这类进程内服务器。它们的工具运行时没有子进程、没有网络往返，并且保留其**裸的** `@tool` 名字 —— 模型看到的是 `echo`，而不是 `mcp__my-tools__echo`。服务器名字给这个包分组；它并不给它加命名空间，所以要挑选不会与内置工具撞车的名字。

## 下一步

- [构建自定义工具](build-custom-tools.md) —— 定义你要打包的那些工具
- [使用 Sandbox](use-sandbox.md) —— 即使在容器下，MCP 仍留在 host 一侧
- [SDK 参考](../reference/sdk.md) —— `McpServerSpec`、`McpHttpServerSpec`、`HostConfig`
- [ADR：MCP connectors](https://github.com/initxy/noeta/blob/main/docs/adr/mcp-connectors.md) —— 客户端的边界，以及凭证存放在哪里

`examples/mcp_server.py` 是一个可运行的进程内 MCP 示例。
