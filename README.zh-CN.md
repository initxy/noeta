# Noeta —— 面向 AI agent 的持久化、provider 中立运行时 + SDK

[English](README.md) · **简体中文**

**[文档站](https://initxy.github.io/noeta/zh/tutorials/first-agent/)** · [你的第一个代理](https://initxy.github.io/noeta/zh/tutorials/first-agent/) · [SDK 参考](https://initxy.github.io/noeta/zh/reference/sdk/) · [对比](https://initxy.github.io/noeta/zh/reference/comparison/)

Noeta 是一个 Python 库，用于在持久化、事件溯源的运行时之上构建长程 agent。它
像 Claude Agent SDK 一样进程内运行 —— 你的代码与引擎之间没有 server、没有 HTTP
—— 每个轮次都是一个被记录下来的引擎任务：崩溃安全、exactly-once，能够为一个人工
回答或一个定时器挂起、并在条件触发时唤醒，全程可审计、可 replay。

## 为什么不自己写这个循环

手写一个绕着 LLM 转的 `while` 循环，会把状态攥在内存里。杀掉进程，任务就没了；
没有任何记录能说明 agent 为何这么做，没法在不阻塞线程的前提下为人工暂停，也没法
在不重写循环的情况下更换模型厂商。Noeta 给你：

- **崩溃安全、精确一次的执行。** 状态从只追加的 event log 里 fold 出来，从不攥
  在内存里 —— 中途杀掉进程，一个全新进程会在准确的那一点恢复，且精确一次。
- **长程挂起/唤醒。** 任务可以为一个人工回答、一个定时器或一个 sub-task 停靠数
  小时甚至数天，然后在条件触发时精确唤醒一次。睡着期间不产生任何成本。
- **完整审计与 replay。** 每个事件、每次 LLM 轮次、每次工具调用、每个 token/cache
  统计都被记录；compaction 是一层可逆的叠加，因此恢复后的任务以同样方式 compact，
  而你仍能读到被削掉的内容。
- **Provider 中立。** Anthropic 与 OpenAI 兼容适配器都在同一套内部协议背后；记录
  下来的历史不绑定任何厂商的形态，内核也被一条 import-linter 规则禁止依赖任何厂商
  SDK。
- **确定性的离线模式。** 脚本化的 `FakeLLMProvider` 无需网络即可驱动整套技术栈，
  因此安装、存储与接线都能在全新 checkout 上以及 CI 里被证明可用。

## 安装

```bash
uv pip install noeta-sdk      # noeta-runtime comes along as a transitive dependency
```

然后 `import noeta.sdk` —— 这一个模块就是全部公开面。Python 3.11+。

## 快速开始 —— 零凭证

不需要 API key，不需要网络。构建一个 `Options` recipe，用 `query` 驱动一个轮次，
provider 用 `noeta.sdk.testing` 里的确定性离线 provider：

<!-- runnable: smoke -->
```python
import tempfile
from pathlib import Path

from noeta.sdk import Options, query, LLMResponse, TextBlock, Usage
from noeta.sdk.testing import FakeLLMProvider

provider = FakeLLMProvider(
    responses=[
        LLMResponse(
            stop_reason="end_turn",
            content=[TextBlock(text="Hello from a Noeta SDK agent!")],
            usage=Usage(uncached=1, output=1),
        )
    ]
)

with tempfile.TemporaryDirectory() as tmp:
    result = query(
        Options(
            system_prompt="You are a concise assistant.",
            name="main",
            allowed_tools=("read",),
            permission_mode="bypassPermissions",
        ),
        goal="Say hello.",
        provider=provider,
        workspace_dir=Path(tmp),
        model="stub-model",
    )
    assert result.answer() == "Hello from a Noeta SDK agent!"
```

`query` 返回这一轮完整的 event-envelope 流 —— agent 所做一切的机器可读记录 ——
`result.answer()` 从末尾那个 envelope 读出答案。换成 `Client` 门面即可进入多轮
会话（`Client.messages`），同一份记录会继续 fold。

## 接入真实模型

provider 是 `Options` 的一个字段 —— **是 wiring，不是 identity** —— 所以替换它
不会改变 agent、它的工具，或它记录下来的历史。适配器通过 `noeta.sdk.providers`
再导出：

```python
from noeta.sdk import Options
from noeta.sdk.providers import AnthropicProvider, OpenAICompatProvider

# api_key falls back to ANTHROPIC_API_KEY / OPENAI_API_KEY when omitted.
# Anthropic requires a token budget: pass default_max_tokens, or max_tokens per request.
anthropic = Options(
    system_prompt="…",
    provider=AnthropicProvider(default_max_tokens=1024),
)
openai = Options(
    system_prompt="…",
    provider=OpenAICompatProvider(base_url="https://api.openai.com/v1"),
)
```

Responses API、第二网关，以及离线测试替身，参见
[配置 provider](https://initxy.github.io/noeta/zh/how-to/configure-provider/)。

## 你能扩展什么

所有开放项都是 `Options` 字段，经 `noeta.sdk` 再导出：

| 缝（seam） | 扩展什么 |
| --- | --- |
| `@tool` | 给一个函数打上 name、version、risk level 和 input schema，使其成为一等工具 |
| `mcp_servers` | 进程内 SDK MCP 工具（`create_sdk_mcp_server`），或连接外部 stdio / HTTP MCP server |
| `provider` | 任意满足 `LLMProvider` 的适配器（provider 中立的基础） |
| `policy` | 把 ReAct 决策函数换成你自己的 |
| `guards` | 在工具调用 / spawn / finish 之前的同步检查 |
| `observers` | 只读的事件订阅者 —— 审计、指标 |
| `content_channels` | 注册一个 `ContentKindSpec`，把自定义常驻内容放进上下文 |

这些贡献打成一束就作为**插件**交付，用 `load_plugins` 加载，并通过 `Options.plugins`
按 agent 选用。可运行的 [`examples/plugins/`](examples/plugins/) 覆盖 guard（受保护
路径、审批模式）、observer（git checkpoint），以及一个 RAG 风格的记忆召回 provider。

## 架构

两个库共享同一个 `noeta.` 命名空间，都在版本 0.4.0：

- **noeta-sdk** —— 你 import 的薄客户端，也是**唯一**的公开面。`query` / `Client`
  / `Options` / `@tool` / `create_sdk_mcp_server`、`noeta.presets` 里的四个官方
  agent、开放的扩展接口（`Tool` / `LLMProvider` / `Policy` / `Guard` / `Observer`
  / `ContentKindSpec`），以及插件加载器。每一项官方能力 —— fs/web 工具包、provider
  适配器、memory、skills、持久化存储后端 —— 都作为 `noeta.builtins` 下的内置插件
  住在这里，只能经由加载器的动态解析触达。
- **noeta-runtime** —— 底下的纯内核：持久化、事件溯源的任务执行，fold/snapshot，
  调度器与 worker lease，以及上下文 composer。它**自身不含任何能力实现**，也不依赖
  任何厂商 SDK。你只会把它作为 `noeta-sdk` 的传递依赖装上，从不直接 import。

任务在 Engine 内部一次推进一步（`compose → decide → dispatch`）。fold 出的状态只
来自只追加的 EventLog 加上内容寻址的 ContentStore，因此任何进程都能仅凭任务的事件
流把它重建出来。一个 Worker 租下一个任务，把它运行到下一个挂起点或终止态，然后
释放；排空循环作为库原语 `noeta.runtime.worker.WorkerLoop` 交付，由嵌入型 host
构造并运行。

## 一个真实的 host，仅由公开面组装

[`examples/reference-host/`](examples/reference-host/) 是能立起一个持久化、插件
扩展、流式 agent 的最小程序 —— 就像一个嵌入型产品会做的那样：持久化 SQLite 存储、
实时 token 流、插件，以及官方 `main` 预设，全部由 `noeta.sdk` /
`noeta.sdk.storage` / `noeta.presets` 组装，**不碰**任何运行时内部。参考 host
能构建 agent，第三方 host 也能。

```bash
python examples/reference-host/host.py   # drives one turn against a scripted offline provider
```

可运行的 [`examples/`](examples/) 端到端覆盖 SDK 面 —— 一个最小 agent、自定义工具、
一个进程内 MCP server、一道权限门、provider 替换、子代理委派，以及在任务中途扛住
`kill -9` —— 每个都带一个离线 smoke 测试，因此它们不会悄悄腐烂。

## 文档

完整文档渲染在
**[initxy.github.io/noeta](https://initxy.github.io/noeta/zh/tutorials/first-agent/)**；
同样的文件位于 [`docs/zh/`](docs/zh/) 下，可直接在源码中浏览。

| 层 | 从这里开始 | 什么时候读 |
| --- | --- | --- |
| 教程（Tutorials） | [你的第一个代理](https://initxy.github.io/noeta/zh/tutorials/first-agent/) | 你是新手，想让它跑起来。 |
| 操作指南（How-to） | [配置 provider](https://initxy.github.io/noeta/zh/how-to/configure-provider/) · [构建自定义工具](https://initxy.github.io/noeta/zh/how-to/build-custom-tools/) · [编写插件](https://initxy.github.io/noeta/zh/how-to/write-a-plugin/) · [部署 Worker](https://initxy.github.io/noeta/zh/how-to/deploy-worker/) | 你有具体任务要完成。 |
| 概念（Concepts） | [事件溯源](https://initxy.github.io/noeta/zh/concepts/event-sourcing/) | 你想理解设计。 |
| 参考（Reference） | [SDK](https://initxy.github.io/noeta/zh/reference/sdk/) · [WorkerLoop](https://initxy.github.io/noeta/zh/reference/worker-loop/) · [对比](https://initxy.github.io/noeta/zh/reference/comparison/) · [工具](https://initxy.github.io/noeta/zh/reference/tools/) | 你需要精确的事实。 |

更深的内容：[架构概览](https://initxy.github.io/noeta/zh/architecture/overview/)、
[故障排查](https://initxy.github.io/noeta/zh/operations/troubleshooting/)，以及记录每个跨模块决策为何如此的
[ADR](https://github.com/initxy/noeta/tree/main/docs/adr)（术语表在 [`CONTEXT.md`](CONTEXT.md)）。

## 贡献

开发设置与仓库布局在 [`CONTRIBUTING.md`](CONTRIBUTING.md)；工作约定（无论人类还是
agent）从根目录的 [`AGENTS.md`](AGENTS.md) 入口开始。

## 许可证

Apache License 2.0 —— 见 [`LICENSE`](LICENSE)。
