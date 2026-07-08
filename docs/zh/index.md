---
layout: home

hero:
  name: "Noeta"
  text: "面向 AI agent 的持久化运行时"
  tagline: 开源、可自托管。任务在崩溃后自动恢复，可暂停等待人工或定时器，并完整记录每一步——Provider 中立，开箱即用离线运行。
  actions:
    - theme: brand
      text: 快速开始
      link: /zh/tutorials/quickstart
    - theme: alt
      text: GitHub
      link: https://github.com/initxy/noeta

features:
  - title: 崩溃自动恢复
    details: 任务状态由仅追加的事件日志重建，而非保存在内存中。在任务执行中途杀掉进程；新进程 fold 日志后继续完成工作——恰好一次。

  - title: 全程可追溯
    details: 每个事件、LLM 轮次、工具调用以及 token/cache 统计都被记录。trace 不仅回答发生了什么，还回答为什么发生。

  - title: 天生支持长周期
    details: 任务可以挂起等待人工、定时器或子任务，在条件触发时恰好唤醒一次。

  - title: Provider 中立
    details: Anthropic 和任何 OpenAI 兼容端点都位于同一内部协议之后。切换供应商只需调整接线，无需重写。

  - title: 自带代理
    details: 运行时负责托管和调度；你提供 policy、工具和上下文。

  - title: 离线优先
    details: 确定性的 stub provider 无需 API key 和网络即可运行整个技术栈。
---
