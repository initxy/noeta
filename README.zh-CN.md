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

**一个给"必须一直跑下去的 agent"用的 Python 运行时 + SDK。** 今天 import 进来，在你自己的进程里驱动一个 agent；明天把同一个 agent 放到多 worker、多 host 的池子上跑 —— agent 本身一行不用改。每一项能力都是插件，每一家模型厂商都只隔着一行接线，每一次运行都持久到能扛住 `kill -9`、事后还能 replay。

[English](README.md) · **简体中文** · [文档站](https://initxy.github.io/noeta/zh/) · [快速开始](https://initxy.github.io/noeta/zh/tutorials/quickstart/) · [你的第一个 agent](https://initxy.github.io/noeta/zh/tutorials/first-agent/) · [SDK 参考](https://initxy.github.io/noeta/zh/reference/sdk/)

## 它落在公开排行榜的第一梯队

| 基准 | 范围 | `noeta-agent` `main`（Claude Opus 4.8） | 领域水平 |
|---|---|---|---|
| Terminal-Bench 2.1 | 40 题分层抽样 | **82.5%**（33/40） | 公开榜单区间 58.7%–83.8% |
| SWE-bench Verified | 15 实例子集 | **86.7%**（13/15） | 榜首约 79%，中段约 66–77% |

跑在 [harbor](https://github.com/harbor-framework/harbor) 上 —— 官方的 Terminal-Bench harness，也是公开排行榜背后的同一套 —— 用官方数据集，由每道题自己的 verifier 打分。参赛的是 [`noeta-agent`](https://github.com/initxy/noeta-agent) 的 `main` 预设，完全由本 SDK 的公开面组装而成，所以这些数字端到端地压到了运行时本身。两行都是**抽样**，并如实标注：是在领域区间里的一个定位，不是全集榜单成绩。

完整方法学、按难度的拆分、排除项，以及可逐字复跑的命令：[基准测试](https://initxy.github.io/noeta/zh/benchmarks/)。

## 为什么选 Noeta

### 面向 server，而不只是一个能调用的循环

`Client.start_workers(n)` 把同一个进程变成常驻 worker 池；把存储指向 Postgres，多个 host 就共享同一个数据库，写入由 lease 围栏保护。Engine 是无状态的 —— 任何 worker 都能推进任何 task，所以横向扩展是换一次存储，不是重写。

```python
with client:
    client.start_workers(4)                       # 常驻池，单进程
    client.start(goal="Ship the release notes.")  # 由某个 worker 接手
```

没有要运维的 daemon，没有 HTTP 跳转，中间也没有厂商服务。进程和数据库都是你自己的。→ [部署 worker](https://initxy.github.io/noeta/zh/how-to/deploy-worker/)

### 每一项能力都是插件 —— 包括我们自己的

内核出厂时**零**能力。文件工具、web 工具、memory、browser、MCP、sandbox、存储后端，以及每一个 provider 适配器，都是内置插件，只经由唯一一道门触达内核：加载器的动态 `ref` 解析。一旦有谁想抄近路，import linter 会让构建失败。

所以你的插件走的路和 Noeta 自己的能力完全一样 —— 不存在一个把你挡在门外的特权内部 API。
→ [编写插件](https://initxy.github.io/noeta/zh/how-to/write-a-plugin/) · [插件 surface](https://initxy.github.io/noeta/zh/reference/plugin-surfaces/)

### 16 个扩展 surface，以惰性数据声明

插件就是一个带**静态 manifest** 的包。在 import 它的任何一行代码**之前**，Noeta 就能列出它贡献了什么，并检查冲突。

| 平面（plane） | Surfaces | 是否进入 agent identity？ |
|---|---|---|
| **Identity** | `tool`、`agent`、`content_kind`、`prompt_fragment`、`policy`、`control_tool` | 是 |
| **Wiring** | `guard`、`observer`、`provider`、`reminder_provider`、`reminder`、`tool_result_transform`、`session_pack` | 否 —— 进程级生效 |
| **Host** | `mcp_server`、`skills`、`sandbox_provider` | 否 —— 由 host 接线 |

### 中途杀掉进程，它会自己恢复

状态从不攥在内存里 —— 它是 `fold(events)`，由只追加的日志重新算出来。worker 持有一份靠 heartbeat 续期的 lease，所以同一个 task 在任何时刻只有一个写者。它一旦死掉，lease 过期，下一个 worker fold 日志，把被打断的那次 attempt 封存为死历史，然后从最后一个持久点继续。精确一次。

### 等待是免费的，而且是一等公民

task 可以为一个人工回答、一个定时器、一个 subtask 或一个外部事件挂起，睡着期间不产生任何成本。唤醒它的 wake 是持久的、单 worker 的、精确一次投递的 —— 所以一个跨月的审批循环和一次五秒的工具调用，用的是同一套机器。

### 任何模型，靠强制而非承诺

Anthropic、任何 OpenAI chat-completions 网关、OpenAI Responses API，都待在同一套从不提及厂商的内部协议后面。更换是 **wiring，不是 identity**：agent、它的工具、它记录下来的历史都不受影响。

```python
from noeta.sdk import Options
from noeta.sdk.providers import AnthropicProvider, OpenAICompatProvider

anthropic = Options(system_prompt="…", provider=AnthropicProvider(default_max_tokens=1024))
openai    = Options(system_prompt="…", provider=OpenAICompatProvider(base_url="https://api.openai.com/v1"))
```

### 可审计是结构决定的

每次模型往返、每次工具调用、每个 guard 裁决、每个 token 计数都是流上的一个事件。compaction 是一层可逆叠加 —— 原文仍在。"agent 做了什么"和"agent 是什么"不可能打架，因为状态只能由 fold 产生。

上面每一条底下的机制，一个想法一页 —— 事件日志、lease、插件加载器：[核心概念](https://initxy.github.io/noeta/zh/concepts/) · [架构](https://initxy.github.io/noeta/zh/architecture/overview/)。

## 60 秒上手

```bash
uv pip install noeta-sdk      # noeta-runtime comes along as a transitive dep
```

Python 3.11+。不需要 API key、不需要网络，就能跑完整的一个轮次 —— 离线的 `FakeLLMProvider` 顶替真实模型：

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
            allowed_tools=("Read",),
            permission_mode="bypassPermissions"),
    goal="Say hello.",
    provider=provider,
    model="stub-model",
)
assert result.answer() == "Hello from Noeta."
```

`Options` 是配方，`query` 驱动一个轮次，返回值携带完整的事件流。这段代码由测试套件在每次运行时真实执行，所以它就是能跑的样子。

下一步：[5 分钟快速开始](https://initxy.github.io/noeta/zh/tutorials/quickstart/) 把这条路继续走到多轮对话、持久化存储和常驻 worker 池。

## 如何扩展

所有开放项要么是一个 `Options` 字段，要么是一条插件贡献。

```python
from noeta.sdk import tool

@tool
def get_weather(city: str) -> str:
    """Get the current weather for a city."""
    return f"{city}: sunny, 22°C"

opts = Options(system_prompt="…", allowed_tools=("Read", get_weather))
```

`allowed_tools` 里工具名和工具对象可以并排放 —— 自定义工具无需任何注册步骤就能加入内置工具集。想把它打包复用，就声明一个 manifest，再按 agent 选用：

```toml
# pyproject.toml — a plugin manifest
[tool.noeta]
name = "my-plugin"
requires-noeta = ">=0.5"

[[tool.noeta.tool]]
name = "get_weather"
ref = "my_plugin.tools:get_weather"
```

```python
from noeta.sdk import load_plugins, Options

plugins = load_plugins(modules=["my_plugin"])
opts = Options(system_prompt="…", plugins=("my-plugin",))
```

完整的 manifest 形状与每一个 surface：[编写插件](https://initxy.github.io/noeta/zh/how-to/write-a-plugin/) · [插件 surface](https://initxy.github.io/noeta/zh/reference/plugin-surfaces/)。

## 横向对比

| | **Noeta** | **Claude Agent SDK** | **Pi Harness** |
|---|---|---|---|
| **定位** | 持久化、面向 server 的 agent 运行时 | 在 Claude 上跑进程内 agent 循环 | 终端 coding agent 壳（TypeScript 工具包） |
| **部署形态** | 多 worker 池；Postgres 上多 host | 客户端库，单进程 | 终端里的单进程 |
| **持久化** | 事件账本 —— `state = fold(log)` | 会话由库托管 | 内存中的会话状态 |
| **挂起 / 唤醒** | 一等公民：人工、定时器、subtask、外部事件 —— 精确一次 | 会话恢复 | 在 TUI 里打断 / 继续 |
| **模型锁定** | 无 —— 接一个适配器即可更换 provider | Claude 优先 | 无 —— 统一的多 provider API |
| **扩展** | 16 个插件 surface + 单写者规则 | 工具、MCP、子代理、hook | TypeScript 包：循环、工具、TUI |

**当 agent 的*运行过程*本身必须是一条能 replay、能审计、能跨 worker 与 host 扩展的账本时，就该用 Noeta** —— 而不只是一个能调用的循环。更长的展开（含 LangGraph 与 Temporal）：[对比](https://initxy.github.io/noeta/zh/reference/comparison/)。

## 了解更多

完整文档：**[initxy.github.io/noeta](https://initxy.github.io/noeta/zh/)**

| | |
|---|---|
| **从这里开始** | [快速开始（5 分钟）](https://initxy.github.io/noeta/zh/tutorials/quickstart/) · [你的第一个 agent](https://initxy.github.io/noeta/zh/tutorials/first-agent/) |
| **上生产** | [部署 worker](https://initxy.github.io/noeta/zh/how-to/deploy-worker/) · [用 Docker 部署](https://initxy.github.io/noeta/zh/how-to/docker-deployment/) · [配置 provider](https://initxy.github.io/noeta/zh/how-to/configure-provider/) · [使用 sandbox](https://initxy.github.io/noeta/zh/how-to/use-sandbox/) |
| **扩展它** | [构建自定义工具](https://initxy.github.io/noeta/zh/how-to/build-custom-tools/) · [编写插件](https://initxy.github.io/noeta/zh/how-to/write-a-plugin/) · [接入 MCP](https://initxy.github.io/noeta/zh/how-to/connect-mcp/) · [派生 subagent](https://initxy.github.io/noeta/zh/how-to/spawn-subagents/) |
| **理解它** | [核心概念](https://initxy.github.io/noeta/zh/concepts/) · [架构](https://initxy.github.io/noeta/zh/architecture/overview/) · [ADR](https://github.com/initxy/noeta/tree/main/docs/adr) |
| **查文档** | [SDK 参考](https://initxy.github.io/noeta/zh/reference/sdk/) · [工具](https://initxy.github.io/noeta/zh/reference/tools/) · [预设代理](https://initxy.github.io/noeta/zh/reference/presets/) · [术语表](https://initxy.github.io/noeta/zh/reference/glossary/) |
| **证据** | [基准测试](https://initxy.github.io/noeta/zh/benchmarks/) · [已知限制](https://initxy.github.io/noeta/zh/operations/limitations/) |

更喜欢读代码？可运行的 [`examples/`](examples/) 覆盖自定义工具、MCP server、权限门、subagent 委派，以及任务中途扛住 `kill -9` —— 每个都带一个离线 smoke 测试；另有 [`examples/reference-host/`](examples/reference-host/)，一个仅由公开面组装起来的完整 host。

## 许可证

Apache 2.0 —— 见 [`LICENSE`](LICENSE)。
