---
layout: home

hero:
  name: "Noeta"
  text: "面向 AI agent 的持久化、provider 中立运行时 + SDK"
  tagline: 一个面向长程 agent 的 Python 库。Task 状态由 append-only 事件日志 fold 而来，因此被杀掉的进程能从中断处恢复；Task 可以为人工、定时器或子任务挂起，并持久化唤醒。import noeta.sdk 即可在进程内驱动引擎 —— 无 server，运行时不需要任何凭证。
  actions:
    - theme: brand
      text: 快速上手（5 分钟）
      link: /zh/tutorials/quickstart
    - theme: alt
      text: 你的第一个 agent
      link: /zh/tutorials/first-agent
    - theme: alt
      text: GitHub
      link: https://github.com/initxy/noeta

features:
  - title: 崩溃可恢复
    details: 一个 Task 的事实基础是它的 append-only 事件日志，而不是驻留内存的某个值。在 Task 执行到一半时杀掉进程，下一个 worker 会 fold 回日志、密封被中断的 Attempt，并且绝不会静默重跑一次已经产生副作用的调用。

  - title: 为长程而设计
    details: Task 可以挂起以等待人、定时器或子任务，休眠期间不消耗任何资源。条件触发时它恰好唤醒一次 —— 匹配是持久化的，因此中途崩溃只会重新投递，而不会把它丢掉。

  - title: 完全可审查
    details: 每个 Step、LLM 往返、工具调用、Guard 裁决和 token 计数都是日志中的一个事件。trace 告诉你某一步为什么发生，而不只是发生了什么。

  - title: 先进程内，再 worker 池
    details: import noeta.sdk 就能在你自己的进程内驱动引擎 —— 没有 HTTP 跳转，也没有需要运维的守护进程。同一份代码可以用 Client.start_workers(n) 扩容，或在 Postgres 上跨多台主机运行，因为 Engine 是无状态的，写入由 lease 围栏保护。

  - title: Provider 中立
    details: Anthropic、任意 OpenAI chat-completions 网关，以及 OpenAI Responses API 都位于同一个从不点名厂商的内部协议之后。切换端点是接线，而不是重写 —— 内核被禁止导入任何 provider 包。

  - title: 每个能力都是插件
    details: 工具、agent、policy、Guard、Observer、MCP server 和 sandbox provider 全都是在十六个扩展 Surface 上以 manifest 声明的贡献。Noeta 自己的 built-in plugin 走的正是你的插件所用的同一个加载器。
---

## 60 秒试用

```bash
uv pip install noeta-sdk      # noeta-runtime 会作为传递依赖一起装上
```

零凭证、零网络 —— 用离线的 `FakeLLMProvider` 驱动一轮：

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

换一个 provider 就能接上真实模型 —— 见
[配置 Provider](/zh/how-to/configure-provider)。

## 找到你要的页面

第一次来？先看[快速上手](/zh/tutorials/quickstart)，再看[你的第一个 agent](/zh/tutorials/first-agent)。其余内容都在下面。

### 教程 —— 边做边学

| 页面 | 你会得到 |
|---|---|
| [快速上手（5 分钟）](/zh/tutorials/quickstart) | 安装、离线跑通一轮、读懂它产生的事件日志。 |
| [你的第一个 agent](/zh/tutorials/first-agent) | 一个带自定义工具和权限闸门的真实 agent。 |
| [CI 集成](/zh/tutorials/ci-integration) | 在 CI 里确定性地跑 agent，不需要 API key。 |

### 操作指南 —— 解决一个具体问题

| 页面 | 什么时候用 |
|---|---|
| [配置 Provider](/zh/how-to/configure-provider) | 你想接真实模型：Anthropic、OpenAI 兼容网关，或 Responses API。 |
| [构建自定义工具](/zh/how-to/build-custom-tools) | agent 需要调用你自己的代码。 |
| [生成子代理](/zh/how-to/spawn-subagents) | 一个 Task 需要把部分工作委派出去并等待结果。 |
| [连接 MCP](/zh/how-to/connect-mcp) | 你想用现成 MCP server 提供的工具。 |
| [编写插件](/zh/how-to/write-a-plugin) | 你想把工具、agent 或 policy 打包复用。 |
| [部署 Worker](/zh/how-to/deploy-worker) | Task 需要在启动它的进程之外继续运行。 |
| [用 Docker 部署](/zh/how-to/docker-deployment) | 你要把 worker 作为容器发布。 |
| [使用 Sandbox](/zh/how-to/use-sandbox) | 工具调用必须与宿主机隔离运行。 |
| [多租户记忆](/zh/how-to/multi-tenant-memory) | 多个租户共用一套部署，且彼此不可见。 |
| [切换 Provider](/zh/how-to/swap-providers) | 已有的 agent 要换到另一个端点。 |

### 核心概念 —— 理解这个模型

| 页面 | 讲什么 |
|---|---|
| [概念总览](/zh/concepts/) | 阅读顺序，以及每个概念的一句话定义。 |
| [事件溯源](/zh/concepts/event-sourcing) | 为什么状态是 `fold(events)`，以及这带来了什么。 |
| [任务模型](/zh/concepts/task-model) | Task 是唯一原语：状态、Attempt、子任务。 |
| [引擎与执行](/zh/concepts/engine-execution) | 一个 Step 做的事：取 lease、fold、组装上下文、决策、派发工具。 |
| [Fold 与快照](/zh/concepts/fold-and-snapshot) | 如何从日志重建状态，以及让它保持快速的快照。 |
| [唤醒与恢复](/zh/concepts/wake-resume) | 为人、定时器或子任务挂起 —— 并恰好唤醒一次。 |
| [Guard 与 Observer](/zh/concepts/guard-observer) | 谁能拦下一次工具调用，谁只能旁观。 |
| [Composer 与缓存](/zh/concepts/composer-and-cache) | prompt 如何按三段组装以命中 provider 缓存。 |
| [Provider 中立](/zh/concepts/provider-neutrality) | 一个内部协议、三个适配器，内核里没有任何厂商。 |

### 架构 —— 它是怎么搭起来的

| 页面 | 覆盖 |
|---|---|
| [概览](/zh/architecture/overview) | 整个系统的导览。 |
| [包与导入规则](/zh/architecture/packages) | `noeta-sdk` 位于 `noeta-runtime` 之上、同一个命名空间，以及隔开二者的规则。 |
| [状态与写入者](/zh/architecture/state-and-writers) | 状态切片、单写入者不变式、带版本的 fold。 |
| [扩展平面](/zh/architecture/extension-planes) | 三个平面上的十六个 Surface，以及 built-in plugin 如何走同一条路。 |

### 参考 —— 查具体细节

| 页面 | 内容 |
|---|---|
| [SDK API 地图](/zh/reference/sdk) | 所有可从 `noeta.sdk` 导入的东西，并链到细节页。 |
| [query / Client](/zh/reference/sdk-client) | 两个入口、它们的参数，以及返回什么。 |
| [Options](/zh/reference/sdk-options) | 每一个 `Options` 字段和各个权限模式。 |
| [类型与测试替身](/zh/reference/sdk-types) | 事件、内容块、结果，以及离线测试替身。 |
| [插件总览](/zh/reference/plugins) | 插件是什么，以及它如何对某个 agent 生效。 |
| [插件 manifest](/zh/reference/plugin-manifest) | manifest 结构、加载过程与版本锁定。 |
| [插件 Surface](/zh/reference/plugin-surfaces) | 全部十六个扩展 Surface，每个一节。 |
| [工具](/zh/reference/tools) | 内置工具清单。 |
| [预设代理](/zh/reference/presets) | 预设 agent 及各自的接线。 |
| [WorkerLoop](/zh/reference/worker-loop) | worker 池 API、lease 与轮询行为。 |
| [对比](/zh/reference/comparison) | Noeta 与其他 agent 框架的对比。 |
| [术语表](/zh/reference/glossary) | 按领域分组的全部术语，附 A–Z 索引。 |

### 运维 —— 让它跑在生产上

| 页面 | 回答 |
|---|---|
| [故障排查](/zh/operations/troubleshooting) | 你真正会遇到的故障：现象、原因、修法。 |
| [已知限制](/zh/operations/limitations) | Noeta 目前还做不到什么，直说。 |
