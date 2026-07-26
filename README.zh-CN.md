# Noeta —— 面向 AI agent 的持久化、provider 中立运行时 + SDK

[English](README.md) · **简体中文**

**[文档站](https://initxy.github.io/noeta/tutorials/first-agent/)** · [你的第一个代理](https://initxy.github.io/noeta/tutorials/first-agent/) · [SDK 参考](https://initxy.github.io/noeta/reference/sdk/) · [对比](https://initxy.github.io/noeta/reference/comparison/)

> **一个用于构建长程 agent 的 Python 库，建在持久化、事件溯源的运行时之上** ——
> 崩溃安全的 exactly-once 执行、面向人工与定时器的挂起/唤醒、worker lease，以及
> 完整的审计与 replay。像 Claude Agent SDK 一样进程内运行：你的代码与引擎之间
> 没有 server、没有 HTTP。零凭证即可完全离线运行；接上 Anthropic 或任意
> OpenAI 兼容端点就能用真实模型。

Noeta 交付**两个库**，共享同一个 `noeta.` 命名空间：

- **noeta-sdk** —— 你 import 的薄客户端，也是**唯一**的公开面。
  `query()` / `Client` / `Options` / `@tool` / `create_sdk_mcp_server`、
  `noeta.presets` 里的四个官方 agent、开放的扩展接口
  （Tool / LLMProvider / Policy / Guard / Observer / ContentChannel），
  以及插件加载器。它不含引擎 —— 只是进程内转发进运行时。
- **noeta-runtime** —— 底下的纯引擎：持久化事件溯源的任务执行、fold/snapshot、
  调度器与 worker lease、内置工具、provider 适配器、上下文 composer。你只会把它
  作为 `noeta-sdk` 的传递依赖装上，从不直接 import。

## 安装

```bash
uv pip install noeta-sdk      # noeta-runtime 会作为传递依赖一起装上
```

然后 `import noeta.sdk` —— 这个模块就是全部公开面。Python 3.11+。

## 快速开始 —— 零凭证

不需要 API key，不需要网络。构建一个 `Options` recipe，用 `query` 驱动一个轮次，
provider 用 `noeta.sdk.testing` 里的确定性离线 provider：

```python
import tempfile
from pathlib import Path

from noeta.sdk import Options, query, LLMResponse, TextBlock, Usage
from noeta.sdk.testing import FakeLLMProvider

# 一个无网络、脚本化在一个轮次里作答的 provider。
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
`result.answer()` 从末尾的 envelope 读出答案。换成 `Client` 门面即可进入多轮
会话（`Client.messages`），同一份记录继续 fold。

## 接入真实模型

provider 是 `Options` 的一个字段 —— **是 wiring，不是 identity** —— 所以替换它
不会改变 agent、它的工具，或它记录下来的历史。适配器通过 `noeta.sdk.providers`
导出：

```python
from noeta.sdk import Options
from noeta.sdk.providers import AnthropicProvider, OpenAICompatProvider

anthropic = Options(system_prompt="…", provider=AnthropicProvider(api_key="sk-ant-…"))
openai = Options(system_prompt="…", provider=OpenAICompatProvider(
    base_url="https://api.openai.com/v1", api_key="sk-…"))
```

Responses API、第二网关，以及离线测试替身，参见
[配置 provider](https://initxy.github.io/noeta/how-to/configure-provider/)。

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

这些贡献打成一束就是**插件**（`load_plugins` / `merge_plugins`），在 compile
之前确定性地合并进 `Options`。可运行的 [`examples/plugins/`](examples/plugins/)
覆盖 guard（受保护路径、审批模式）与 observer（git checkpoint）。

## 一个真实的 host，仅由公开面组装

[`examples/reference-host/`](examples/reference-host/) 是能立起一个持久化、
插件扩展、流式 agent 的最小程序 —— 就像一个嵌入型产品会做的那样：持久化 SQLite
存储、实时 token 流、插件，以及官方 `main` 预设，全部由 `noeta.sdk` /
`noeta.sdk.storage` / `noeta.presets` 组装，**不碰**任何运行时内部。参考 host
能构建 agent，第三方 host 也能。

```bash
python examples/reference-host/host.py   # 对着一个脚本化的离线 provider 驱动一个轮次
```

## 底下的运行时

每个轮次都是一个持久化、事件溯源的引擎任务：

- **崩溃安全、精确一次的执行。** 状态从只追加的 event log 里 fold 出来，从不
  攥在内存里 —— 中途杀掉进程，新进程从准确的那一点恢复，精确一次。
- **长程任务。** 任务可以挂起数小时甚至数天，等一个人工回答、定时器或 sub-task，
  条件满足时精确唤醒一次 —— 睡着时不产生任何成本。排空循环作为库原语交付
  （`noeta.runtime.worker.WorkerLoop`），由嵌入者构造并运行（见
  [部署 Worker](https://initxy.github.io/noeta/how-to/deploy-worker/)）。
- **完整审计与 replay。** 每个事件、每次 LLM 调用、每次工具调用、每个 token/cache
  统计都被记录；compaction 是可逆的叠加层，因此恢复后的任务以同样方式 compact，
  而且你仍能读到被削掉的内容。
- **Provider 中立。** Anthropic 与 OpenAI 兼容适配器都在同一套内部协议背后 ——
  记录下来的历史不绑定任何厂商的形态，内核也被一条 import-linter 规则禁止依赖
  任何厂商 SDK。
- **确定性的离线模式。** 脚本化的 `FakeLLMProvider` 让整套技术栈无网络跑通，
  因此安装、存储、接线都能在全新 checkout（以及 CI）上验证。

## 只用你需要的那一层

| 包 | 你得到什么 | 类比 |
| --- | --- | --- |
| `noeta-sdk` | 你 import 的客户端门面：`query()`、`Client`、`Options`、`@tool`、预设、扩展接口。 | Claude Agent SDK |
| `noeta-runtime` | 纯引擎 —— event log、fold、调度器、工具、policy、provider。你从不直接 import 的传递依赖。 | —— |

可运行的 [`examples/`](examples/) 端到端覆盖 SDK 面 —— 最小 agent、自定义工具、
进程内 MCP server、权限门、provider 替换、子代理委派，以及在任务中途扛住
`kill -9` —— 每个都带一个离线 smoke 测试，因此不会悄悄腐烂。

## 文档

完整文档渲染在
**[initxy.github.io/noeta](https://initxy.github.io/noeta/tutorials/first-agent/)**。
同样的文件位于 [`docs/`](docs/) 下，可直接在源码中浏览。

| 层 | 从这里开始 | 什么时候读 |
| --- | --- | --- |
| 教程（Tutorials） | [你的第一个代理](https://initxy.github.io/noeta/tutorials/first-agent/) | 你是新手，想让它跑起来。 |
| 操作指南（How-to） | [配置 provider](https://initxy.github.io/noeta/how-to/configure-provider/) · [构建自定义工具](https://initxy.github.io/noeta/how-to/build-custom-tools/) · [编写插件](https://initxy.github.io/noeta/how-to/write-a-plugin/) | 你有具体任务要完成。 |
| 概念（Concepts） | [事件溯源](https://initxy.github.io/noeta/concepts/event-sourcing/) | 你想理解设计。 |
| 参考（Reference） | [SDK](https://initxy.github.io/noeta/reference/sdk/) · [WorkerLoop](https://initxy.github.io/noeta/reference/worker-loop/) · [对比](https://initxy.github.io/noeta/reference/comparison/) · [工具](https://initxy.github.io/noeta/reference/tools/) | 你需要精确的事实。 |

更深的内容：[架构概览](https://initxy.github.io/noeta/architecture/overview/)、
[故障排查](https://initxy.github.io/noeta/operations/troubleshooting/)，以及记录每个跨模块决策缘由的
[ADR](https://github.com/initxy/noeta/tree/main/docs/adr)（术语表在 [`CONTEXT.md`](CONTEXT.md)）。

## 贡献

开发设置和仓库布局在 [`CONTRIBUTING.md`](CONTRIBUTING.md)；工作约定（人类或
agent）从根目录的 [`AGENTS.md`](AGENTS.md) 入口开始。

## 许可证

Apache License 2.0 —— 见 [`LICENSE`](LICENSE)。
