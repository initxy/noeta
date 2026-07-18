---
layout: home

hero:
  name: "Noeta"
  text: "构建在持久化运行时之上的多用户 agent 平台"
  tagline: 开源、可自托管。会话（session）与协作空间（space）、每会话一个的沙箱，以及一个在崩溃后依然存活、记录每一步的事件溯源引擎——Provider 中立，开箱即可离线运行。
  actions:
    - theme: brand
      text: 快速开始
      link: /zh/tutorials/quickstart
    - theme: alt
      text: GitHub
      link: https://github.com/initxy/noeta

features:
  - title: 崩溃自动恢复
    details: 任务状态从仅追加的事件日志重建，从不驻留内存。任务中途杀掉进程；新进程把日志 fold 回来并完成剩下的工作——精确一次。

  - title: 全程可追溯
    details: 每个事件、LLM 轮次、工具调用和 token/cache 统计都被记录。trace 回答的是某一步为什么发生，而不只是发生了什么。

  - title: 天生支持长周期
    details: 任务可以挂起等待人工、定时器或子任务，并在条件触发时被精确唤醒一次。

  - title: Provider 中立
    details: Anthropic 和任何 OpenAI 兼容端点都位于同一套内部协议之后。切换厂商只是改接线，不是重写。

  - title: 为团队而生
    details: 个人与团队空间中的多用户会话；skill、知识、记忆和 MCP 连接器都以空间为作用域——执行始终发生在每会话专属的沙箱里。

  - title: 离线优先
    details: 确定性的 mock provider 加上 dev-login，无需 API key、无需网络即可运行整个平台。
---
