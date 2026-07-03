# Noeta

**面向 AI 代理的单主机、持久化、事件溯源运行时。**

Noeta 负责代理的托管、记录与调度——但不规定代理的编写方式。代理的每一步都写入只追加的 **EventLog**，任务的全部状态由该日志 *fold（折叠）* 还原。挂起与恢复、崩溃恢复、回放、exactly-once 唤醒并非后加的功能，而是将日志视为唯一真相来源后的自然结果。

进程内代理库（Claude Agent SDK、LangChain）为你提供执行循环，而 Noeta 在其下方增加了持久化基底——因此代理的历史是一份可以 fold、检查和重入的日志，而非随进程消亡的瞬时内存。

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

    90 秒冒烟测试，然后是真实 provider 演练。

    [:octicons-arrow-right-24: 从这里开始](getting-started.md)

-   :material-lightbulb-on-outline:{ .lg .middle } **核心概念**

    ---

    Task、EventLog、Engine、Dispatcher、Guard、Observer——Noeta 背后的模型。

    [:octicons-arrow-right-24: 了解模型](concepts.md)

-   :material-console:{ .lg .middle } **Noeta 代理**

    ---

    附带的编码代理：工具、预设、技能、权限模型、HTTP 接口。

    [:octicons-arrow-right-24: 使用代理](noeta-agent.md)

-   :material-api:{ .lg .middle } **API 参考**

    ---

    通过 mkdocstrings 从 Python docstring 自动生成。

    [:octicons-arrow-right-24: 浏览 API](reference/api/index.md)

</div>

## 架构

关于自上而下的架构演练——事件溯源 Engine、三包布局、provider adapter、上下文组合——请参阅[架构深入](noeta-architecture-deep-dive.md)。

关于跨模块决策的原因，请浏览[架构决策记录](adr/index.md)。
