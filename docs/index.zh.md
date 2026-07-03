# Noeta

**面向 AI agent 的开源可自托管运行时。Provider 中立、事件溯源、为持久化而生。**

Noeta 把 Claude Code 或 Claude Agent SDK 里的 agent 执行循环，架设在一条持久化、可审查、事件溯源的脊柱上——不绑定任何单一厂商，也不规定 agent 该怎么写。

agent 走的每一步都会落入一份只追加的 **EventLog**，而一个任务的完整状态由这份日志 *fold（折叠）* 回来。挂起与恢复、崩溃恢复、replay、exactly-once wake 都不是额外加上去的功能；它们是"把日志当作唯一事实来源"这一前提的自然结果。

如果说进程内 agent 库（Claude Agent SDK、LangChain）给你的是那个执行循环，那么 Noeta 在它下面补上了持久化底座——agent 的历史是一份你可以 fold、检查、重新进入的日志，而不是随进程消失的临时内存。

## 为什么选择 Noeta

- **构建即持久化** —— 每次状态变化都是一条追加事件；任务状态由日志确定性 fold 得出，从不跨运行持有。进程在任务中途被杀，fold 会立即将其恢复。
- **提供者中立** —— Anthropic 和 OpenAI 兼容端点是同一内部协议背后的 adapter。切换提供者只是接线，而非重写。
- **自带代理** —— 运行时负责托管和调度；你提供 policy、工具和上下文。内置 ReAct policy 和一个编码代理，但你完全可以不用。
- **离线优先** —— 确定性的 `stub` provider 无需 API key 和网络即可运行整个技术栈，因此安装、存储和接线在全新 checkout（以及 CI 中）即可验证。
- **按需使用层级** —— 嵌入内核、导入 SDK，或运行开箱即用的编码代理及其附带的 Web UI。

## 快速开始（无需 API key）

`stub` provider 是一个确定性的两轮 LLM 替身——无需 key，无需网络。

```bash
# 安装编码代理（传递性拉取 SDK + runtime）。
uv pip install -e apps/noeta-agent
python -m noeta.agent   # 启动离线 stub 编码代理 + 附带 Web UI
```

或从仓库根目录：

```bash
make install   # 首次：editable 安装 + Web 依赖
make run        # 构建 Web + 启动后端（离线 stub，端口 8765）
#  → 打开 http://127.0.0.1:8765/chat
```

## 界面预览

<figure markdown>
  ![附带的 Web 应用——运行中任务的聊天界面](assets/web-app.png){ width="840" }
  <figcaption>附带的 Web 应用——运行中任务的聊天界面。</figcaption>
</figure>

<figure markdown>
  ![每任务 trace 视图——fold 后的事件流](assets/trace.png){ width="840" }
  <figcaption>每任务 trace 视图——fold 后的事件流。</figcaption>
</figure>

## 接下来去哪里

<div class="grid cards" markdown>

-   :material-rocket-launch:{ .lg .middle } **快速开始**

    ---

    5 分钟离线冒烟测试——安装、启动 stub agent、查看 trace。

    [:octicons-arrow-right-24: 从这里开始](tutorials/quickstart.md)

-   :material-lightbulb-on-outline:{ .lg .middle } **核心概念**

    ---

    事件溯源、任务模型、引擎与执行、Fold 与快照、唤醒与恢复等。

    [:octicons-arrow-right-24: 了解模型](concepts/event-sourcing.md)

-   :material-console:{ .lg .middle } **Noeta 代理**

    ---

    附带的 coding agent：工具、预设、技能、权限模型、环境配置。

    [:octicons-arrow-right-24: 使用代理](reference/noeta-agent.md)

-   :material-api:{ .lg .middle } **API 参考**

    ---

    SDK API、HTTP 路由、WorkerLoop、预设、工具、术语表。

    [:octicons-arrow-right-24: 浏览 API](reference/sdk.md)

</div>

## 架构

关于自上而下的架构演练——事件溯源 Engine、三包布局、provider adapter、上下文组合——请参阅[架构概览](architecture/overview.md)。

关于跨模块决策的原因，请浏览[架构决策记录](adr/index.md)。
