---
layout: home

hero:
  name: "Noeta"
  text: "面向 AI agent 的持久化、provider 中立运行时 + SDK"
  tagline: 一个用于构建长程 agent 的 Python 库，建在持久化、事件溯源的运行时之上 —— 崩溃安全的 exactly-once 执行、面向人工与定时器的挂起/唤醒，以及完整的审计与 replay。像 Claude Agent SDK 一样进程内运行；零凭证即可离线运行。
  actions:
    - theme: brand
      text: 你的第一个代理
      link: /zh/tutorials/first-agent
    - theme: alt
      text: SDK 参考
      link: /zh/reference/sdk
    - theme: alt
      text: GitHub
      link: https://github.com/initxy/noeta

features:
  - title: 崩溃可恢复
    details: 任务状态由 append-only 事件日志重建，从不驻留内存。任务执行到一半杀掉进程；新进程 fold 回日志并完成工作 —— exactly-once。

  - title: 完全可审查
    details: 每个事件、LLM 轮次、工具调用与 token/缓存统计都被记录。trace 回答某一步为什么发生，而不只是发生了什么。

  - title: 为长程而设计
    details: 任务可以挂起以等待人工、定时器或子任务，并在条件触发时 exactly-once 唤醒。

  - title: Provider 中立
    details: Anthropic 与任意 OpenAI 兼容端点都在同一个内部协议之后。切换厂商是接线，不是重写。

  - title: 进程内，无 server
    details: import noeta.sdk 即可进程内驱动引擎 —— 你的代码与运行时之间没有 HTTP，就像 Claude Agent SDK。

  - title: 离线优先
    details: 一个确定性 mock provider 让整个 SDK 无需 API key、无需网络即可运行。
---
