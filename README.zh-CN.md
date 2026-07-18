# Noeta — 构建在持久化运行时之上的多用户 agent 平台

[English](README.md) · **简体中文**

**[文档站](https://initxy.github.io/noeta/)** · [快速开始](https://initxy.github.io/noeta/tutorials/quickstart/) · [平台参考](https://initxy.github.io/noeta/reference/noeta-agent/) · [SDK 参考](https://initxy.github.io/noeta/reference/sdk/)

> **一个可自托管、面向团队的 agent 服务** —— 多用户会话（session）与协作空间（space）、每会话一个的沙箱容器、按空间划分的 skill / 知识 / 记忆 / MCP 连接器、一个管理员控制台 —— 全部构建在一条**持久化、事件溯源的运行时**之上，具备完整审计与 replay。零凭证即可完全离线运行；接上任意 OpenAI-Responses 兼容网关就能用真实模型。

Noeta 在一个仓库里交付两样东西：

- **平台**（`noeta-agent`）—— 一个可部署的多用户 agent 服务：FastAPI 后端加
  React SPA，作为单个进程交付。用户登录后在**空间**（个人或团队）中工作，与
  agent 进行**会话**；agent 的执行只发生在每会话专属的 Docker 沙箱里。空间承载
  agent 的 skill、知识源、长期记忆、MCP 连接器和配置；管理员拥有带用量统计和
  原始事件 trace 的控制台。
- **运行时 + SDK**（`noeta-runtime`、`noeta-sdk`）—— 底下的库：持久化事件溯源
  的任务执行、崩溃安全的 exactly-once 恢复、面向人工与定时器的挂起/唤醒、
  worker lease、完整审计与 replay。`noeta.sdk` 是进程内构建你自己的 agent 的
  唯一公开 import 面。

## 快速开始 —— 零凭证，60 秒

不需要 API key、不需要 Docker、不需要注册账号。从一份全新 checkout 开始
（Python 3.11+ 配 [uv](https://docs.astral.sh/uv/)，Node 20+）：

```bash
git clone https://github.com/initxy/noeta && cd noeta
make install   # uv sync + 前端依赖
make run       # 构建 SPA + 在 http://127.0.0.1:8000 启动平台
```

打开 <http://127.0.0.1:8000>，用**任意用户名**登录（dev-login），发一条消息。
没有配置 LLM 时，平台运行确定性的 **mock provider**：一段脚本化的对话会把真实
机制完整走一遍 —— 一个澄清提问、一次 skill 激活、一份写回的回答 —— 全程离线。
想用显式命令代替 `make`：

```bash
uv sync
cd apps/web && npm ci && npm run build && cd ../..
uv run python -m noeta.agent
```

同样的组装过程，用代码写出来：

<!-- runnable: smoke -->
```python
from noeta.agent.main import create_app

# 完全离线的默认值：确定性 mock LLM、SQLite 应用存储、dev-login。
# create_app 只组装 FastAPI 应用，不启动服务。
app = create_app()
assert "/api/v1/health" in app.openapi()["paths"]
```

## 接入真实模型

平台对接任意 **OpenAI-Responses 兼容网关**。在 `apps/noeta-agent/.env` 里配置
（可从 `.env.example` 复制）：

```dotenv
LLM_PROVIDER=auto            # auto = 配好网关就用网关，否则回落到离线 mock
LLM_BASE_URL=https://your-gateway.example.com/v1
LLM_API_KEY=sk-…
```

`LLM_BASE_URL` 是网关根地址 —— provider 会自动追加 `/responses`。用户可选的
模型菜单在 `apps/noeta-agent/models.json`（id、显示名、推理力度档位）；可选的
第二网关（`SECONDARY_LLM_BASE_URL` / `SECONDARY_LLM_API_KEY`）服务其中标记
`"gateway": "secondary"` 的模型。参见
[`examples/openai-compatible/`](examples/openai-compatible/) 的即抄即用配置，
以及[配置参考](https://initxy.github.io/noeta/reference/configuration/)的完整键表。

## 打开沙箱

执行**在设计上就是仅沙箱的**：agent 的 shell 和文件副作用只发生在每会话专属的
Docker 容器里，绝不落在宿主机上 —— 没有宿主机 shell 工具，也没有逐调用审批流程。
没有 Docker 时平台降级为纯对话模式（shell 执行关闭），零凭证快速开始用的正是
这个模式。

```dotenv
SANDBOX_ENABLED=true
```

整个开关就这一行 —— 它需要本地 Docker daemon，并拉取现成的
[AIO Sandbox 镜像](https://github.com/agent-infra/sandbox)
（`ghcr.io/agent-infra/sandbox`）。之后每个会话拿到自己的容器：会话工作区
以读写方式 bind-mount，空间的知识与 skill 以只读方式挂载，web UI 从同一容器
实时串流 **Browser / Terminal / Code** 面板。空闲容器分两级回收（先 stop，再
remove）；恢复的会话会重新挂回自己的容器。
[`examples/deployment/`](examples/deployment/) 提供 docker-compose 封装。

## 空间给 agent 带来什么

agent 带进会话的一切都以它所在的空间为作用域，在 UI（或
[HTTP API](https://initxy.github.io/noeta/reference/http-api/)）中管理：

- **Skill** —— 可上传的 `SKILL.md` 包，模型按需激活；平台级内置 skill 由管理员
  管理，空间 skill 由空间所有者管理。
- **知识源** —— 同步的 `git_repo` / `local_dir` 内容，只读挂载进沙箱，引用可
  溯源回原始位置。
- **agent 记忆** —— 每空间一个基于文件的长期记忆池，由 agent 自己的工具写入，
  成员可浏览、可编辑。
- **MCP 连接器** —— 每空间的 MCP server（`http` 或 `stdio`），可按连接器裁剪
  工具子集；凭证绝不离开服务端。
- **agent-config** —— 人设 prompt、默认模型与推理力度、知识选择、记忆开关。
- **模板与工作流** —— 可复用的 prompt 模板，以及节点间自动生成交接文档的多节点
  工作流会话。
- **反馈闭环** —— 成员为消息评分；分析 agent 把评分转成建议，所有者决定采纳
  （写入记忆或作为 skill 补丁）或导出为 markdown 报告。

## 底下的运行时

平台正是它所构建的引擎的官方演练场 —— 每个会话轮次都是一个持久化、事件溯源的
引擎任务：

- **崩溃安全、精确一次的执行。** 状态从只追加的 event log 里 fold 出来，从不
  攥在内存里 —— 中途杀掉进程，新进程从准确的那一点恢复，精确一次。
- **长程任务。** 任务可以挂起数小时甚至数天，等一个人工回答、定时器或
  sub-task，条件满足时被*精确唤醒一次* —— 睡着时不产生任何成本。
- **完整审计与 replay。** 每个事件、每次 LLM 调用、每次工具调用、每个
  token/cache 统计都被记录；compaction 是可逆的叠加层。管理员 trace 视图回答
  的是某一步*为什么*发生，而不只是*发生了什么*。
- **Provider 中立。** Anthropic 与 OpenAI 兼容适配器都在同一套内部协议背后
  —— 记录下来的历史不绑定任何厂商的形态。
- **确定性的离线模式。** mock provider 加 dev-login 让整套技术栈无网络跑通，
  因此安装、存储、接线都能在全新 checkout（以及 CI）上验证。

前后端之间的 wire 有意**不是**原始 event log：后端把引擎事件翻译成稳定、带版本
的 UI 事件词汇表，通过每会话一条 SSE 流下发；replay 靠从日志重新推导
（`since_seq`）而非存一份投影。原始 envelope 只保留在管理员 trace 面上。
参见[平台参考](https://initxy.github.io/noeta/reference/noeta-agent/)与
[server-platform ADR](docs/adr/server-platform-product.md)。

## 诚实的边界

平台的第一个版本把自己的边界写明白：

- **单进程、单实例。** 应用状态是 SQLite；水平扩展是后续工作。
- **默认认证是 dev-login** —— 任意用户名、签名 cookie。它是开发用的便利；
  真实部署应把身份系统接入可插拔的 `AuthProvider` 缝。
- **尚无限流与配额。**
- **沙箱隔离是「进程 + 挂载 FS」**，不是完整牢笼。

## 只用你需要的那一层

| 包 | 你得到什么 | 类比 |
| --- | --- | --- |
| `noeta-runtime` | 纯引擎 —— event log、fold、调度器、工具、policy。进程内嵌入。 | —— |
| `noeta-sdk` | 你 import 的客户端门面：`query()`、`Client`、`Options`、`@tool`。 | Claude Agent SDK |
| `noeta-agent` | 多用户 agent 平台：FastAPI 后端 + web SPA + 沙箱宿主。 | —— |

装 `noeta-sdk`（`uv pip install noeta-sdk`）来构建你自己的 agent ——
`import noeta.sdk` 是唯一的公开面，底下的引擎是你从不直接碰的传递依赖。
运行平台则按上文从 checkout 启动。可运行的 [`examples/`](examples/) 两边都覆盖。

## 文档

完整文档渲染在 **[initxy.github.io/noeta](https://initxy.github.io/noeta/)**。同样的文件位于 [`docs/`](docs/) 下，可直接在源码中浏览。

| 层 | 从这里开始 | 什么时候读 |
| --- | --- | --- |
| 教程（Tutorials） | [快速开始](https://initxy.github.io/noeta/tutorials/quickstart/) | 你是新手，想让它跑起来。 |
| 操作指南（How-to） | [使用平台](https://initxy.github.io/noeta/how-to/use-the-coding-agent/) | 你有具体任务要完成。 |
| 概念（Concepts） | [事件溯源](https://initxy.github.io/noeta/concepts/event-sourcing/) | 你想理解设计。 |
| 参考（Reference） | [平台参考](https://initxy.github.io/noeta/reference/noeta-agent/) · [HTTP API](https://initxy.github.io/noeta/reference/http-api/) · [配置](https://initxy.github.io/noeta/reference/configuration/) · [SDK](https://initxy.github.io/noeta/reference/sdk/) | 你需要精确的事实。 |

更深的内容：[架构概览](https://initxy.github.io/noeta/architecture/overview/)、
[故障排查](https://initxy.github.io/noeta/operations/troubleshooting/)，以及记录每个跨模块决策缘由的
[ADR](https://initxy.github.io/noeta/adr/)（术语表在 [`CONTEXT.md`](CONTEXT.md)）。

## 贡献

开发设置和仓库布局在 [`CONTRIBUTING.md`](CONTRIBUTING.md)；工作约定（人类或
agent）从根目录的 [`AGENTS.md`](AGENTS.md) 入口开始。`make check` 是本地 CI
门禁；`make e2e-web` 运行可选的浏览器 e2e 套件。

## 许可证

Apache License 2.0 —— 见 [`LICENSE`](LICENSE)。
