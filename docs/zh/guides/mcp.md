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
| 两者 | `call_timeout_s` | 一次 `tools/call` 最多等多少秒；`None` 即 30 秒；0 或负数报错。其他请求仍用默认值。它也是连接池 key 的一部分 |
| 两者 | `deferred` | 设为 `True` 时，这个服务器的工具 schema 不再随每个请求发给模型，模型改用 `ToolSearch` / `McpCall` 查找和调用（见下文）。默认 `False`；不是布尔值会报错 |

服务器如果下发了 `Mcp-Session-Id`（Streamable HTTP），之后每个请求都会带上它；SSE 回复里对应的响应一到就返回。stdio 服务器发来的 `ping` 会得到回应。想换成自己的 HTTP 传输，设置 `HostConfig.mcp_http_post`。

## 工具多的服务器：先不发 schema

模型能看到的每个工具，完整的 JSON schema 都要随每个请求发一遍。一个有 40 个工具的服务器，每个请求能多出 25 KB 左右，而这些 schema 模型大多用不上。在它的 spec 上设 `deferred=True`，模型看到的就换成两个小工具，这一轮所有设了 `deferred` 的服务器共用这一对：

- `ToolSearch(query)`：返回最多 5 个匹配的工具，带名字、说明和完整的输入 schema。按名字和说明里的词匹配，也可以直接给完整名字；`query` 为空时列出全部这类工具，每个一行说明。
- `McpCall(tool, arguments)`：调用指定的工具。这次调用在任何 guard 检查之前就换成对真实 `mcp__{alias}__{tool}` 的调用，所以权限规则、`require_approval_tools`、`can_use_tool` 和审计记录看到的都是真实工具名。名字不存在、名字对应的工具没设 `deferred`、或者缺少 `arguments` 对象，都只让这一次调用报错，同一批里的其他调用照常执行。

```python
"crm": McpHttpServerSpec(alias="crm", url="https://crm.example.com/mcp", deferred=True),
```

一轮之内工具集合仍然不变，提示词缓存不受影响。代价是模型第一次用到某个工具时，要多一个来回去查它的 schema。工具多、偶尔才用的服务器适合这样设；工具少或者一直在用的，保持原样就好。

## 模型拿到什么

列工具等列表请求会跟着 `nextCursor` 翻页（最多 100 页），工具多的服务器也能列全。工具结果保留文字；`structuredContent` 也会带上（太大的会另存，用 `structured_content_ref` 引用）；模型能显示的图片以图片形式交给模型；其他非文字内容写成一行文字。

## 服务器出错或配置变了

- 某个服务器在一轮开始时连不上，就跳过它，记一条 `McpServerSkipped` 事件，这一轮用其余服务器继续。alias 重复会直接抛 `McpConfigError`。
- 工具名超过 64 个字符时会被截短，加上 8 位 sha256 后缀，`mcp__<alias>__` 前缀保留。同一个服务器里两个工具名清洗后撞上，本来就合法的那个保持原名，其余的加后缀；不会因为一个名字就丢掉整个服务器。两个不同服务器的工具最后撞成同一个名字，会在构建 agent 时抛 `McpConfigError`，并写出两个 alias。空名字仍然报错。
- 调用超时或服务器断开时，这条连接会退役，下一轮重新连。
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
