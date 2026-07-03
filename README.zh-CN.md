# Noeta

[English](README.md) · **简体中文**

**[文档站](https://initxy.github.io/noeta/zh/)** · [快速开始](https://initxy.github.io/noeta/zh/tutorials/quickstart/) · [SDK 参考](https://initxy.github.io/noeta/zh/reference/sdk/) · [配置 provider](https://initxy.github.io/noeta/zh/how-to/configure-provider/)

> 面向 AI agent 的开源可自托管运行时。Provider 中立、事件溯源、为持久化而生。

Noeta 把 Claude Code 或 Claude Agent SDK 里的 agent 执行循环，架设在一条持久化、可审查、事件溯源的脊柱上——不绑定任何单一厂商，也不规定 agent 该怎么写。

agent 走的每一步都会落入一份只追加的 **EventLog**，而一个任务的完整状态由这份日志 *fold（折叠）* 回来。挂起与恢复、崩溃恢复、replay、exactly-once wake 都不是额外加上去的功能；它们是"把日志当作唯一事实来源"这一前提的自然结果。

如果说进程内 agent 库（Claude Agent SDK、LangChain）给你的是那个执行循环，那么 Noeta 在它下面补上了持久化底座——agent 的历史是一份你可以 fold、检查、重新进入的日志，而不是随进程消失的临时内存。

<p align="center">
  <img src="docs/assets/web-app.png" alt="Noeta coding-agent web 应用" width="820">
  <br>
  <em>内置 coding-agent web 应用，由 <code>python -m noeta.agent</code> 启动。</em>
</p>

<p align="center">
  <img src="docs/assets/trace.png" alt="Noeta 逐任务 trace 视图" width="820">
  <br>
  <em>逐任务 trace 视图——每个事件、每次 LLM 调用、每个 token/cache 统计，直接来自 EventLog。</em>
</p>

## 为什么选择 Noeta

- **构造即持久** —— 每次状态变化都是一个追加事件；任务状态由日志确定性地 fold 得出，从不跨运行保留。杀掉运行中的进程，fold 立刻把它恢复回来。
- **provider 中立** —— Anthropic 和 OpenAI 兼容端点都是同一套内部协议背后的适配器。切换 provider 只是改接线，不是重写；任何厂商的形态都不会渗入核心。
- **自带 agent** —— 运行时负责托管和调度，你提供 policy、工具和上下文。仓库内置了一个 ReAct policy 和一个 coding agent，但你不必用它们。
- **离线优先** —— 确定性的 `stub` provider 让你在没有 API key 和网络的情况下跑通完整技术栈，因此安装、存储和接线都可以在全新 checkout 上验证（也可以在 CI 里验证）。
- **按需取用** —— 嵌入内核（`noeta-runtime`）、导入 SDK（`noeta-sdk`）、或者运行包含 web UI 的完整 coding agent（`noeta-agent`）。每层都自动拉入它下面的依赖。

## 快速开始

```bash
# 尚未发布到 PyPI——从 git 安装 coding agent（自动拉入 SDK + runtime）：
pip install "noeta-agent @ git+https://github.com/initxy/noeta.git#subdirectory=apps/noeta-agent"
python -m noeta.agent   # 启动离线 stub coding agent + 内置 web UI
```

不需要 API key——默认的 `stub` provider 是一个确定性 LLM 替身。打开打印出的 URL 并发一条消息。同样的启动，用代码写出来：

<!-- runnable: smoke -->
```python
from noeta.agent.backend.lifecycle import BackendConfig, serve_backend

# 默认完全离线：两轮 stub provider，:memory: 存储。
# port=0 绑定一个操作系统分配的端口。工作目录是当前目录。
config = BackendConfig(port=0)
server, url, shutdown = serve_backend(config)
try:
    assert url.startswith("http://")
finally:
    shutdown()
```

引导式路径——安装、运行、打开 web UI、查看 trace——请看[快速开始教程](https://initxy.github.io/noeta/zh/tutorials/quickstart/)。要接入真实的 Anthropic 或 OpenAI 兼容模型，请看[配置 provider](https://initxy.github.io/noeta/zh/how-to/configure-provider/)。要在 SDK 上构建自己的 agent——定义 `@tool`、组装 `Options`、调用 `query()`——从[你的第一个 agent](https://initxy.github.io/noeta/zh/tutorials/first-agent/)和可运行的 [`examples/`](examples/)开始。

它和 Claude Agent SDK 比怎么样？两者都给你 agent 循环、工具、MCP 和 sub-agent；区别在底下的脊梁——请看[服务端对比](https://initxy.github.io/noeta/zh/reference/comparison/)。

## 文档

完整文档渲染在 **[initxy.github.io/noeta](https://initxy.github.io/noeta/zh/)**（中文路径）。同样的文件以 `*.zh.md` 与英文一起位于仓库的 [`docs/`](docs/) 下，可直接在源码中浏览。

| 层 | 从这里开始 | 什么时候读 |
| --- | --- | --- |
| 教程（Tutorials） | [快速开始](https://initxy.github.io/noeta/zh/tutorials/quickstart/) | 你是新手，想让它跑起来。 |
| 操作指南（How-to） | [配置 provider](https://initxy.github.io/noeta/zh/how-to/configure-provider/) | 你有具体任务要完成。 |
| 概念（Concepts） | [事件溯源](https://initxy.github.io/noeta/zh/concepts/event-sourcing/) | 你想理解设计。 |
| 参考（Reference） | [SDK 参考](https://initxy.github.io/noeta/zh/reference/sdk/) | 你需要精确的 API 事实。 |

更深的内容：[架构概览](https://initxy.github.io/noeta/zh/architecture/overview/)、[故障排查](https://initxy.github.io/noeta/zh/operations/troubleshooting/)，以及记录每个跨模块决策的 [ADR](https://initxy.github.io/noeta/zh/adr/)（术语表在 [`CONTEXT.md`](CONTEXT.md)）。

## 状态与范围

Noeta 处于早期 pre-1.0 预览阶段。它能跑、有测试、核心稳定，但部分能力目前有意不在范围内：

- **单机 / 单 worker。** 随附的 worker 在进程内排空 dispatcher，它是预览，不是生产守护进程。单 worker 持久 exactly-once wake 已交付；多主机协调、多 worker  fencing 和 partial-step-orphan 边界（步骤中途崩溃）仍未解决——见[已知限制](https://initxy.github.io/noeta/zh/operations/limitations/)。
- **Human-in-the-loop / 定时器 wake** —— 引擎已具备形态，完整 UX 仍在落地中。
- **前端** —— 随附的 web 应用是一个小型 Vite MPA，使用原生 ES 模块；预览阶段不计划迁移到任何框架。

## 贡献

开发设置和仓库布局在 [`CONTRIBUTING.md`](CONTRIBUTING.md)；工作约定（人类或 agent）从根目录的 [`AGENTS.md`](AGENTS.md) 入口开始。

## 许可证

Apache License 2.0——见 [`LICENSE`](LICENSE)。
