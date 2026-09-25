# 接入 MCP 服务器

让 agent 用上任意 MCP（Model Context Protocol）服务器提供的工具：可以是每轮按需连接的外部 `stdio` / HTTP 服务器，也可以把你自己写的 `@tool` 函数打包成进程内服务器。

| | 外部服务器 | 进程内服务器 |
| --- | --- | --- |
| 在哪配置 | `HostConfig.mcp_server_resolver` | `Options.mcp_servers` |
| 何时启用 | 每轮按 alias 指定（`enabled_mcp=`） | 对这个 agent 一直生效 |
| 模型看到的工具名 | `mcp__{alias}__{tool}` | `@tool` 原来的名字 |
| 在哪运行 | 子进程，或走 HTTP | 你的进程里 |

## 接外部服务器

给 host 一个查找函数（`alias -> spec | None`），然后每轮说明要连哪些 alias：

```python
from noeta.sdk import Client, HostConfig, McpHttpServerSpec, McpServerSpec, Options
from noeta.sdk.providers import AnthropicProvider

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
    Options(system_prompt="You are a helpful assistant.", name="my-agent"),
    provider=AnthropicProvider(),
    model="claude-sonnet-5",
    host_config=HostConfig(mcp_server_resolver=SERVERS.get),
)

outcome = client.start(goal="List the data directory.", enabled_mcp=("fs",))
```

这一轮模型能看到 `mcp__fs__read_file` 和 `mcp__fs__list_directory`。

- `enabled_mcp` 只管当前这一轮，不写进任务配置。`start`、`send_goal`、`seed_start`、`seed_send_goal` 都接受这个参数。`query()` 不接受，要接外部 MCP 得用 `Client`。
- 凭证（`headers`、`env`）只存在于你构造的 spec 里，不会进事件日志，也不会给模型看到。
- MCP 工具就是普通工具：guard、权限模式、审批对它们照常生效。

## spec 字段

| spec | 字段 | 说明 |
| --- | --- | --- |
| 两种都有 | `alias` | 必须匹配 `^[a-z0-9_-]{1,32}$` |
| 两种都有 | `tool_subset` | 要保留的原始工具名；`None` 表示全部保留 |
| `McpServerSpec` | `argv` | 启动命令，直接执行，不经过 shell |
| `McpServerSpec` | `env` | 给子进程加的环境变量，写成 `(("KEY", "value"), …)` |
| `McpHttpServerSpec` | `url` | 唯一的 JSON-RPC 地址 |
| `McpHttpServerSpec` | `headers` | 每个请求都带的固定请求头，写成 `(("Name", "value"), …)` |

服务器如果下发了 `Mcp-Session-Id`（Streamable HTTP），之后每个请求都会带上它。想换成自己的 HTTP 传输，设置 `HostConfig.mcp_http_post`。

## 服务器出错或配置变了

- 某个服务器在一轮开始时连不上，就跳过它，记一条 `McpServerSkipped` 事件，这一轮用其余服务器继续。alias 重复会直接抛 `McpConfigError`。
- 工具名超过 64 个字符或者重名，会立刻报错，不会悄悄截断。
- 连接按 host 放进连接池，各任务共用。空闲连接在 `HostConfig.mcp_idle_ttl`（默认 1800 秒）后关闭。改了某个服务器的配置，调用 `client.reconnect_mcp("fs")`（不带参数就是全部重连）。
- 同时服务多个租户？设置 `HostConfig.mcp_scope_resolver`，有状态的服务器就不会被不同租户共用。见[按租户隔离记忆](multi-tenant-memory.md)。

## 让子 agent 也能用

子 agent 只有在定义里启用了 `mcp`，才会拿到这一轮启用的服务器：

```python
AgentDefinition(description="…", prompt="…", plugins=("mcp",))
```

## 把自己的工具打包成进程内服务器

```python
from noeta.sdk import Options, ToolContext, ToolResult, create_sdk_mcp_server, tool

@tool(
    name="echo",
    version="1",
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

options = Options(system_prompt="…", name="my-agent", mcp_servers=(my_mcp,))
```

这些工具就在你的进程里跑，没有子进程也没有网络往返，名字也保持原样：模型看到的是 `echo`，不是 `mcp__my-tools__echo`。起名时注意别和内置工具撞。可运行的例子见 `examples/mcp_server.py`。

## 下一步

- [自定义工具](tools.md)：写要打包的 `@tool` 函数
- [沙箱](sandbox.md)：工具跑在容器里时，MCP 仍然留在 host 上
- [ADR：MCP connectors](https://github.com/initxy/noeta/blob/main/docs/adr/mcp-connectors.md)
