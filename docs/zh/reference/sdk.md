# SDK 参考：`noeta.sdk`

`noeta.sdk` 是你唯一需要导入的模块。客户端动词、agent 配方、扩展接口和插件机制全都从它重新导出，因此应用代码永远不必伸手去够 `noeta.client`、`noeta.core` 或任何其他内部包。

```python
from noeta.sdk import query, Client, Options, tool
```

这些页面上每个名字的事实来源，是 `packages/noeta-sdk/noeta/sdk/__init__.py` 里的 `__all__` 列表。不在 `__all__` 里的名字就不是公开的，未来的版本可能会挪动它。

## 各部分在哪里

这个接口面足够大，所以被拆成了三个聚焦的页面。挑与你的问题相符的那一个。

| 页面 | 涵盖 | 什么时候用它 |
| --- | --- | --- |
| [query / Client](sdk-client.md) | `query`、`QueryResult`、`Client` 及其所有动词、常驻 worker 池、检查、类型化的错误接口 | 你在*驱动*一个 agent——开始一轮、批准一次工具调用、恢复一场对话 |
| [Options](sdk-options.md) | `Options`、`AgentDefinition`、`SystemPromptPreset`、`compile_options`、权限模式、插件激活，以及 `HostConfig` 宿主接线 | 你在*配置*一个 agent——用哪些工具、用什么 prompt、用哪个存储后端 |
| [类型与测试](sdk-types.md) | 扩展接口、消息与内容类型、`LLMResponse` / `Usage`、`@tool` 编写 API、`noeta.sdk.testing` | 你在*实现*某个 Noeta 会回调进来的东西——一个工具、一个 provider、一个 guard |

另有两个参考页面与它们并列：[插件](plugins.md) 讲打包与发现机制，[预设代理](presets.md) 讲 `noeta.presets` 里的四个官方 agent。

## 子模块

`noeta.sdk` 下有四个名字是独立的模块，而不是根级重新导出，因为导入它们会拖进大多数调用方并不需要的重量。

| 模块 | 包含 | 为什么单独放 |
| --- | --- | --- |
| `noeta.sdk.providers` | `AnthropicProvider`、`OpenAICompatProvider`、`OpenAIResponsesProvider`、`CATALOG`、`ModelSpec` | 只有真正构建网络 provider 的调用方才为 `httpx` 付费 |
| `noeta.sdk.storage` | `open_storage_stack` 以及 sqlite / postgres 适配器 | 只有选了 Postgres 的调用方才为 `psycopg` 付费 |
| `noeta.sdk.testing` | `FakeLLMProvider` | 测试用料绝不能从一个生产导入路径够到 |
| `noeta.presets` | 四个官方 agent 及其 prompt | 以 `presets` 从根重新导出，也可以直接导入 |

四者都是惰性解析的，因此没有任何东西静态导入 `noeta.builtins`——正是这条规则让内核不含厂商代码。

## 最短的完整程序

```python
from noeta.sdk import Options, query
from noeta.sdk.providers import AnthropicProvider

options = Options(system_prompt="You are a concise coding assistant.")

result = query(
    options,
    goal="List the Python files in this directory.",
    provider=AnthropicProvider(api_key="sk-ant-…"),
    workspace_dir=".",
)
print(result.answer())
# → 'docs/conf.py, setup.py, …'  (the agent's terminal answer, as a str)
```

`query` 是一次性路径。要做跨多轮的对话，请改为构建一个 `Client`——见 [query / Client](sdk-client.md)。

## 下一步

- [快速上手](../tutorials/quickstart.md) —— 五分钟跑起一个 agent
- [你的第一个 agent](../tutorials/first-agent.md) —— 一个带自定义工具和权限的真实 agent
- [包与导入规则](../architecture/packages.md) —— 为什么是两个 wheel、一条导入路径
- [术语表](glossary.md) —— 这些页面上的每个术语，只定义一次
