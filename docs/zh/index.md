---
layout: home

hero:
  name: "Noeta"
  text: "面向 AI agent 的持久化、provider 中立运行时 + SDK"
  tagline: 一个面向长程 agent 的 Python 库。Task 状态由 append-only 事件日志 fold 而来，因此被杀掉的进程能从中断处恢复；Task 可以为人工、定时器或子任务挂起，并持久化唤醒。import noeta.sdk 即可在进程内驱动引擎 —— 无 server，运行时不需要任何凭证。
  actions:
    - theme: brand
      text: 你的第一个 agent
      link: /zh/tutorials/first-agent
    - theme: alt
      text: SDK 参考
      link: /zh/reference/sdk
    - theme: alt
      text: GitHub
      link: https://github.com/initxy/noeta

features:
  - title: 崩溃可恢复
    details: 一个 Task 的事实基础是它的 append-only 事件日志，而非驻留内存中的某个值。在 Task 执行到一半时杀掉进程；下一个 worker 会 fold 回日志、密封被中断的 Attempt，然后要么重新驱动它，要么停放等待人工处理 —— 它绝不会静默重跑一次带副作用的调用。

  - title: 完全可审查
    details: 每个 Step、LLM 往返、工具调用、Guard 裁决，以及每轮的 token/cache 计数都是日志中的一个事件。trace 回答某一步为什么发生，而不只是发生了什么。

  - title: 为长程而设计
    details: Task 会挂起以等待人、定时器或子任务，并在条件触发时恰好唤醒一次 —— 匹配是持久化的，因此唤醒与恢复之间的崩溃只会重新投递，而不会丢失。

  - title: Provider 中立
    details: Anthropic、任意 OpenAI chat-completions 网关，以及 OpenAI Responses API 都位于同一个从不点名厂商的内部协议之后。切换端点是接线，而非重写 —— 内核被禁止导入任何 provider 包。

  - title: 进程内，无 server
    details: 安装 noeta-sdk，import noeta.sdk，即可在你自己的进程内驱动引擎。你的代码与运行时之间没有 HTTP 跳转，也没有需要运维的守护进程。

  - title: 开箱即用的离线运行
    details: noeta.sdk.testing 中的 FakeLLMProvider 让整个 SDK 无需 API key、无需网络即可确定性地运行 —— 这正是引擎自身测试套件所用的那个替身。

  - title: 每个能力都是插件
    details: 工具、agent、policy、Guard、Observer、MCP server 和 sandbox provider 全都是在十六个扩展 Surface 上以 manifest 声明的贡献。Noeta 自己的 built-in plugin 走的正是你的插件所用的同一个加载器。
---
