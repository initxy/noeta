<div align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="docs/assets/logo/noeta-logo-dark.svg">
    <img src="docs/assets/logo/noeta-logo-light.svg" alt="Noeta — 一条事件日志 fold 成状态" width="336">
  </picture>
  <p>
    <a href="https://pypi.org/project/noeta-sdk/"><img alt="PyPI" src="https://img.shields.io/pypi/v/noeta-sdk"></a>
    <a href="https://github.com/initxy/noeta/actions/workflows/ci.yml"><img alt="CI" src="https://github.com/initxy/noeta/actions/workflows/ci.yml/badge.svg?branch=main"></a>
    <a href="https://pypi.org/project/noeta-sdk/"><img alt="Python versions" src="https://img.shields.io/pypi/pyversions/noeta-sdk"></a>
    <a href="LICENSE"><img alt="License: Apache-2.0" src="https://img.shields.io/badge/license-Apache--2.0-blue"></a>
  </p>
</div>

**一个 Python 运行时 + SDK：agent 的整段运行就是一条可 replay 的事件账本。** Noeta 把长程 agent 托管在你自己的进程里 —— 没有 server，没有 HTTP 跳转 —— 并把每一次模型往返、每一次工具调用、每一次审批都记成事件。状态从不攥在内存里，它是 `fold(events)`，由日志重新算出来。中途杀掉进程，另一个 worker 会从它停下的那一点精确恢复，且精确一次。内核在结构上被禁止 import 任何厂商 SDK，所以 Anthropic、OpenAI 兼容、Responses 这几种模型之间只隔着一行接线。

[English](README.md) · **简体中文** · [文档站](https://initxy.github.io/noeta/zh/) · [快速开始](https://initxy.github.io/noeta/zh/tutorials/quickstart/) · [你的第一个 agent](https://initxy.github.io/noeta/zh/tutorials/first-agent/) · [SDK 参考](https://initxy.github.io/noeta/zh/reference/sdk/)

## 60 秒上手

```bash
uv pip install noeta-sdk      # noeta-runtime comes along as a transitive dep
```

Python 3.11+。接着，不需要 API key、不需要网络，就能跑完整的一个轮次 —— 离线的 `FakeLLMProvider` 顶替真实模型：

```python
from noeta.sdk import Options, query, LLMResponse, TextBlock, Usage
from noeta.sdk.testing import FakeLLMProvider

provider = FakeLLMProvider(responses=[
    LLMResponse(stop_reason="end_turn",
                content=[TextBlock(text="Hello from Noeta.")],
                usage=Usage(uncached=1, output=1))
])

result = query(
    Options(system_prompt="You are concise.",
            allowed_tools=("read",),
            permission_mode="bypassPermissions"),
    goal="Say hello.",
    provider=provider,
    model="stub-model",
)
assert result.answer() == "Hello from Noeta."
```

`Options` 是配方，`query` 驱动一个轮次，返回值携带完整的事件流 —— `result.answer()` 只是从末尾把答案读出来。这段代码由测试套件在每次运行时真实执行，所以它就是能跑的样子。

换成真实模型。provider 是 **wiring，不是 identity**：换掉它不会改变 agent、它的工具，或它记录下来的历史。

```python
from noeta.sdk import Options
from noeta.sdk.providers import AnthropicProvider, OpenAICompatProvider

anthropic = Options(system_prompt="…", provider=AnthropicProvider(default_max_tokens=1024))
openai    = Options(system_prompt="…", provider=OpenAICompatProvider(base_url="https://api.openai.com/v1"))
```

下一步：[5 分钟快速开始](https://initxy.github.io/noeta/zh/tutorials/quickstart/) 把这条路继续走到多轮对话、持久化存储和常驻 worker 池。

## 它是怎么工作的

三个想法撑起了整个设计。其余的一切 —— 审计、replay、挂起 / 恢复、provider 中立 —— 都是从它们里长出来的。

### 1. 状态是事件日志的一次 fold

<p align="center">
  <img src="docs/assets/diagrams/event-sourcing.svg" alt="Event sourcing — 事件追加进 EventLog，大对象进 ContentStore，fold 重建四个状态切片" width="820">
</p>

每个 task 拥有一条只追加的事件流：交给它的目标、每次组装出的 context plan、每次模型响应、每次工具调用及其结果、每次挂起与唤醒。没有一张供引擎读写的 task 表。谁需要当前状态，就从头 fold 这条流把它算出来 —— 状态对象是一份可丢弃的投影，日志才是母本。事件 payload 保持很小（上限 4 KB）；更大的东西，比如一整个响应体或一份大的工具输出，会进入内容寻址的存储，事件里只留一个引用。因为状态只能由 fold 产生，“agent 做了什么”和“agent 是什么”永远不会打架。

深入：[事件溯源](https://initxy.github.io/noeta/zh/concepts/event-sourcing/) · [fold 与 snapshot](https://initxy.github.io/noeta/zh/concepts/fold-and-snapshot/) · [状态与写者](https://initxy.github.io/noeta/zh/architecture/state-and-writers/)

### 2. 中途杀掉它，它会恢复

<p align="center">
  <img src="docs/assets/diagrams/crash-resume.svg" alt="崩溃与恢复 —— worker A 在步骤中途死亡，lease 过期，worker B fold 日志并精确一次地继续" width="820">
</p>

一个 worker 对 task 取得一份 **lease** —— 一个短期的、靠 heartbeat 续期的独占持有 —— 并把它推进到下一个挂起点或终止态。每一次向日志写入都要出示这个 lease id，所以同一个 task 在任何时刻只可能有一个写者。worker 一旦死掉，heartbeat 停止，lease 过期，task 回到就绪队列；下一个 worker fold 日志，把被打断的那次 attempt 封存为死历史，然后从最后一个持久点继续。同一套机制也覆盖有意的等待：task 可以为一个人工回答、一个定时器或一个 subtask 挂起，睡着期间不产生任何成本，而唤醒它的 wake 是持久的、单 worker 的、精确一次投递的 —— 至少一次投递加上幂等消费。

深入：[唤醒与恢复](https://initxy.github.io/noeta/zh/concepts/wake-resume/) · [任务模型](https://initxy.github.io/noeta/zh/concepts/task-model/) · [部署 worker](https://initxy.github.io/noeta/zh/how-to/deploy-worker/)

### 3. 两个包，能力即插件

<p align="center">
  <img src="docs/assets/diagrams/architecture.svg" alt="Noeta 架构 —— 你的代码 import noeta.sdk，其下是 noeta-runtime 内核，builtins 只经插件加载器触达内核" width="820">
</p>

Noeta 以两个库交付，共享同一个 `noeta.` 命名空间。**`noeta-sdk`** 是你唯一要 import 的东西：`query` / `Client` / `Options` / `@tool`、预设 agent，以及每一项官方能力。**`noeta-runtime`** 是纯内核 —— Engine、fold、snapshot、Worker、Dispatcher、lease、context composer —— 它没有声明任何依赖。内核自身不含任何能力：文件工具、web 工具、memory、browser、MCP、sandbox、存储后端，以及每一个 provider 适配器，都是内置**插件**，只能经由加载器的动态 ref 解析触达内核，这条规则由 import linter 在每次构建时强制执行。正是这一道边界，让 provider 中立成为结构性事实而不是一句承诺，也让你的插件走的路和 Noeta 自己的完全一样。

深入：[架构概览](https://initxy.github.io/noeta/zh/architecture/overview/) · [两个包](https://initxy.github.io/noeta/zh/architecture/packages/) · [扩展平面](https://initxy.github.io/noeta/zh/architecture/extension-planes/)

## 为什么选 Noeta

| | 你能得到什么 |
|---|---|
| **崩溃安全** | 状态是 `fold(events)`，从不攥在内存里。中途杀掉进程 —— 下一个 worker 从它停下的那一点恢复，且精确一次。 |
| **面向 server** | `Client.start_workers(n)` 拉起常驻 worker 池；在 Postgres 上多个 host 共享一个数据库，写入由 lease 围栏保护。Engine 无状态，任何 worker 都能推进任何 task。 |
| **长程** | task 可以为一个人工回答、一个定时器或一个 subtask 挂起，条件触发时持久唤醒。睡着不花钱。 |
| **Provider 中立** | Anthropic、OpenAI 兼容、Responses 适配器都在同一套协议背后。内核不能 import 厂商 SDK —— 一旦尝试，构建就失败。 |
| **可审计** | 每次模型往返、每次工具调用、每个 guard 裁决、每个 token 计数都是事件。compaction 是一层可逆叠加，原文仍留在流上。 |
| **可扩展** | 16 个由 manifest 声明的扩展 surface。Noeta 自己的内置能力（fs、web、memory、browser、MCP……）与你的插件走同一个加载器。 |

### Noeta vs Claude Agent SDK vs Pi Harness

| | **Noeta** | **Claude Agent SDK** | **Pi Harness** |
|---|---|---|---|
| **定位** | 持久化、面向 server 的 agent 运行时 | 在 Claude 上跑进程内 agent 循环 | 终端 coding agent 壳（TypeScript 工具包） |
| **部署形态** | 多 worker 池；Postgres 上多 host | 客户端库，单进程 | 终端里的单进程 |
| **持久化** | 事件账本 —— `state = fold(log)` | 会话由库托管 | 内存中的会话状态 |
| **挂起 / 唤醒** | 一等公民：人工、定时器、subtask、外部事件 —— 精确一次 | 会话恢复 | 在 TUI 里打断 / 继续 |
| **模型锁定** | 无 —— 接一个适配器即可更换 provider | Claude 优先 | 无 —— 统一的多 provider API |
| **扩展** | 16 个插件 surface + 单写者规则 | 工具、MCP、子代理、hook | TypeScript 包：循环、工具、TUI |
| **审计 / replay** | 完整事件日志，fold 可复现 | 会话转录 | 会话转录 |

**当 agent 的*运行过程*本身必须是一条能 replay、能审计、能跨 worker 与 host 扩展的账本时，就该用 Noeta** —— 而不只是一个能调用的循环。更长的展开（含 LangGraph 与 Temporal）：[对比](https://initxy.github.io/noeta/zh/reference/comparison/)。

## 如何扩展

所有开放项要么是一个 `Options` 字段，要么是一条插件贡献。Noeta 自己的能力走的是同一条路。

### 加一个工具

```python
from noeta.sdk import tool

@tool
def get_weather(city: str) -> str:
    """Get the current weather for a city."""
    return f"{city}: sunny, 22°C"

opts = Options(system_prompt="…", allowed_tools=("read", get_weather))
```

`allowed_tools` 里工具名和工具对象可以并排放，所以自定义工具无需任何注册步骤就能加入内置工具集。

### 加一个插件

插件是一个携带**静态 manifest** 的包，manifest 声明它对 16 个 surface 中任意一个的贡献。manifest 是惰性数据 —— 在 import 它的任何一行代码之前，Noeta 就能列出并检查冲突。

| 平面（plane） | Surfaces | 是否进入 agent identity？ |
|---|---|---|
| **Identity** | `tool`、`agent`、`content_kind`、`prompt_fragment`、`policy`、`control_tool` | 是 |
| **Wiring** | `guard`、`observer`、`provider`、`reminder_provider`、`reminder`、`tool_result_transform`、`session_pack` | 否 —— 进程级生效 |
| **Host** | `mcp_server`、`skills`、`sandbox_provider` | 否 —— 由 host 接线 |

```toml
# pyproject.toml — a plugin manifest
[tool.noeta]
name = "my-plugin"
requires-noeta = ">=0.5"

[[tool.noeta.tool]]
name = "get_weather"
ref = "my_plugin.tools:get_weather"
```

把它加载进进程，然后按 agent 选用：

```python
from noeta.sdk import load_plugins, Options

plugins = load_plugins(modules=["my_plugin"])
opts = Options(system_prompt="…", plugins=("my-plugin",))
```

完整的 manifest 形状与每一个 surface：[编写插件](https://initxy.github.io/noeta/zh/how-to/write-a-plugin/) · [插件 surface](https://initxy.github.io/noeta/zh/reference/plugin-surfaces/)。

## 了解更多

完整文档：**[initxy.github.io/noeta](https://initxy.github.io/noeta/zh/)**

**教程（Tutorials）** —— 从这里开始，按顺序读
[快速开始](https://initxy.github.io/noeta/zh/tutorials/quickstart/) · [你的第一个 agent](https://initxy.github.io/noeta/zh/tutorials/first-agent/) · [CI 集成](https://initxy.github.io/noeta/zh/tutorials/ci-integration/)

**操作指南（How-to）** —— 一页一件事
[配置 provider](https://initxy.github.io/noeta/zh/how-to/configure-provider/) · [更换 provider](https://initxy.github.io/noeta/zh/how-to/swap-providers/) · [构建自定义工具](https://initxy.github.io/noeta/zh/how-to/build-custom-tools/) · [编写插件](https://initxy.github.io/noeta/zh/how-to/write-a-plugin/) · [接入 MCP](https://initxy.github.io/noeta/zh/how-to/connect-mcp/) · [派生 subagent](https://initxy.github.io/noeta/zh/how-to/spawn-subagents/) · [使用 sandbox](https://initxy.github.io/noeta/zh/how-to/use-sandbox/) · [部署 worker](https://initxy.github.io/noeta/zh/how-to/deploy-worker/) · [用 Docker 部署](https://initxy.github.io/noeta/zh/how-to/docker-deployment/) · [多租户 memory](https://initxy.github.io/noeta/zh/how-to/multi-tenant-memory/)

**概念（Concepts）** —— 为什么这样设计
[总览](https://initxy.github.io/noeta/zh/concepts/) · [事件溯源](https://initxy.github.io/noeta/zh/concepts/event-sourcing/) · [fold 与 snapshot](https://initxy.github.io/noeta/zh/concepts/fold-and-snapshot/) · [任务模型](https://initxy.github.io/noeta/zh/concepts/task-model/) · [Engine 与执行](https://initxy.github.io/noeta/zh/concepts/engine-execution/) · [唤醒与恢复](https://initxy.github.io/noeta/zh/concepts/wake-resume/) · [composer 与缓存](https://initxy.github.io/noeta/zh/concepts/composer-and-cache/) · [Guard 与 Observer](https://initxy.github.io/noeta/zh/concepts/guard-observer/) · [provider 中立](https://initxy.github.io/noeta/zh/concepts/provider-neutrality/)

**参考（Reference）** —— 精确的事实
[SDK](https://initxy.github.io/noeta/zh/reference/sdk/) · [插件](https://initxy.github.io/noeta/zh/reference/plugins/) · [工具](https://initxy.github.io/noeta/zh/reference/tools/) · [预设 agent](https://initxy.github.io/noeta/zh/reference/presets/) · [WorkerLoop](https://initxy.github.io/noeta/zh/reference/worker-loop/) · [对比](https://initxy.github.io/noeta/zh/reference/comparison/) · [术语表](https://initxy.github.io/noeta/zh/reference/glossary/)

**架构与运维**
[架构概览](https://initxy.github.io/noeta/zh/architecture/overview/) · [故障排查](https://initxy.github.io/noeta/zh/operations/troubleshooting/) · [已知限制](https://initxy.github.io/noeta/zh/operations/limitations/) · [ADR](https://github.com/initxy/noeta/tree/main/docs/adr)

更喜欢读代码？可运行的 [`examples/`](examples/) 覆盖自定义工具、MCP server、权限门、subagent 委派，以及任务中途扛住 `kill -9` —— 每个都带一个离线 smoke 测试；另有 [`examples/reference-host/`](examples/reference-host/)，一个仅由公开面组装起来的完整 host。

## 许可证

Apache 2.0 —— 见 [`LICENSE`](LICENSE)。
