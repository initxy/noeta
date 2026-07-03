---
layout: home

hero:
  name: "Noeta"
  text: "面向 AI agent 的持久化运行时"
  tagline: |
    开源、可自托管、Provider 中立、事件溯源。
    为长周期、任务导向的 agent 而生。
  actions:
    - theme: brand
      text: 快速开始
      link: /zh/tutorials/build-a-research-agent
    - theme: alt
      text: 查看 GitHub
      link: https://github.com/initxy/noeta

features:
  - icon: 🛡️
    title: 构建即持久化
    details: 每次状态变化都是一条追加事件。任务状态由日志确定性 fold 得出，从不跨运行持有。进程在任务中途被杀，fold 会立即将其恢复。
    link: /zh/reference/glossary
    linkText: 了解术语

  - icon: 🔌
    title: Provider 中立
    details: Anthropic 和 OpenAI 兼容端点是同一内部协议背后的 adapter。切换提供者只是接线，而非重写。
    link: /zh/reference/configuration
    linkText: 查看配置

  - icon: 🤖
    title: 自带代理
    details: 运行时负责托管和调度；你提供 policy、工具和上下文。内置 ReAct policy 和编码代理，但你完全可以不用。
    link: /zh/reference/tools
    linkText: 浏览工具

  - icon: 🧪
    title: 离线优先
    details: 确定性的 stub provider 无需 API key 和网络即可运行整个技术栈，安装、存储和接线在全新 checkout 及 CI 中即可验证。
    link: /zh/tutorials/ci-integration
    linkText: CI 集成

  - icon: 🧩
    title: 按需使用层级
    details: 嵌入内核、导入 SDK，或运行开箱即用的编码代理及其附带的 Web UI。
    link: /zh/reference/presets
    linkText: 查看预设

  - icon: 📜
    title: 事件溯源真相
    details: agent 走的每一步都会落入只追加的 EventLog。挂起与恢复、崩溃恢复、replay、exactly-once wake 都不是额外加上去的功能。
    link: /zh/reference/http-api
    linkText: HTTP 接口
---

## 界面预览

<figure>
  <img src="../assets/web-app.png" alt="附带的 Web 应用——运行中任务的聊天界面" style="max-width: 840px; border-radius: 8px;">
  <figcaption style="text-align: center; color: var(--vp-c-text-2); margin-top: 8px;">附带的 Web 应用——运行中任务的聊天界面。</figcaption>
</figure>

<figure>
  <img src="../assets/trace.png" alt="每任务 trace 视图——fold 后的事件流" style="max-width: 840px; border-radius: 8px;">
  <figcaption style="text-align: center; color: var(--vp-c-text-2); margin-top: 8px;">每任务 trace 视图——fold 后的事件流。</figcaption>
</figure>
