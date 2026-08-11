---
layout: home

hero:
  name: "Noeta"
  text: "给「必须一直跑下去的 agent」用的 Python 运行时 + SDK"
  tagline: 今天在你自己的进程里驱动一个 agent；明天把同一个 agent 放到多 worker、多 host 的池子上跑 —— agent 本身一行不用改。每一项能力都是插件，每一家模型厂商都只隔着一行接线，每一次运行都持久到能扛住 kill -9、事后还能 replay。
  actions:
    - theme: brand
      text: 快速上手（5 分钟）
      link: /zh/tutorials/quickstart
    - theme: alt
      text: 基准测试
      link: /zh/benchmarks
    - theme: alt
      text: GitHub
      link: https://github.com/initxy/noeta

features:
  - title: 面向 server，而不只是一个能调用的循环
    details: Client.start_workers(n) 把同一个进程变成常驻 worker 池；把存储指向 Postgres，多个 host 就共享同一个数据库，写入由 lease 围栏保护。Engine 是无状态的，所以横向扩展是换一次存储、不是重写 —— 而且没有要运维的 daemon，也没有 HTTP 跳转。

  - title: 每一项能力都是插件 —— 包括我们自己的
    details: 内核出厂时零能力。文件工具、web 工具、memory、browser、MCP、sandbox、存储后端，以及每一个 provider 适配器，都是内置插件，只经由唯一一道门触达内核。你的插件走的是同一条路；不存在一个把你挡在门外的特权内部 API。

  - title: 16 个扩展 Surface，以惰性数据声明
    details: 插件就是一个带静态 manifest 的包，所以在 import 它的任何一行代码之前，Noeta 就能列出它贡献了什么并检查冲突。工具、agent、policy、Guard、Observer、MCP server、sandbox provider 全都是贡献。

  - title: 中途杀掉进程，它会自己恢复
    details: 状态从不攥在内存里 —— 它是 fold(events)，由只追加的日志重新算出来，靠 heartbeat 续期的 lease 保证每个 Task 只有一个写者。下一个 worker 会封存被打断的 Attempt，并从最后一个持久点精确一次地继续。

  - title: 等待是免费的，而且是一等公民
    details: Task 可以为一个人工回答、一个定时器、一个子任务或一个外部事件挂起，睡着期间不产生任何成本。唤醒是持久的、单 worker 的、精确一次投递的 —— 跨月的审批循环和五秒的工具调用用的是同一套机器。

  - title: 任何模型，靠强制而非承诺
    details: Anthropic、任意 OpenAI chat-completions 网关，以及 OpenAI Responses API 都位于同一个从不点名厂商的内部协议之后。切换端点是接线、不是重写 —— 内核被禁止导入任何 provider 包，一旦尝试，构建就失败。
---

## 落在公开排行榜的第一梯队

| 基准 | 范围 | `noeta-agent` `main`（Claude Opus 4.8） | 领域水平 |
|---|---|---|---|
| Terminal-Bench 2.1 | 40 题分层抽样 | **82.5%**（33/40） | 公开榜单区间 58.7%–83.8% |
| SWE-bench Verified | 15 实例子集 | **86.7%**（13/15） | 榜首约 79%，中段约 66–77% |

跑在 [harbor](https://github.com/harbor-framework/harbor)（官方 Terminal-Bench
harness）上，用官方数据集，由每道题自己的 verifier 打分。参赛的是
[`noeta-agent`](https://github.com/initxy/noeta-agent) 的 `main` 预设，完全由本
SDK 的公开面组装而成。两行都是**抽样**，并如实标注 —— 完整方法学、排除项与可复跑命令见[基准测试](/zh/benchmarks)。

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
            allowed_tools=("Read",),
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
| [基准测试](/zh/benchmarks) | 建在 Noeta 上的 agent 在公开基准上的成绩，以及这是怎么测的。 |
