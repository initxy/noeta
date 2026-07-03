---
layout: home

hero:
  name: "Noeta"
  text: "面向 AI agent 的持久化运行时"
  tagline: 开源、可自托管、Provider 中立、事件溯源。
  actions:
    - theme: brand
      text: 快速开始
      link: /zh/tutorials/build-a-research-agent
    - theme: alt
      text: GitHub
      link: https://github.com/initxy/noeta

features:
  - title: 构建即持久化
    details: 每次状态变化都是一条追加事件。进程在任务中途被杀，fold 会立即将其恢复。

  - title: Provider 中立
    details: Anthropic 和 OpenAI 兼容端点是同一协议背后的 adapter。

  - title: 自带代理
    details: 运行时负责托管和调度；你提供 policy、工具和上下文。

  - title: 离线优先
    details: 确定性的 stub provider 无需 API key 和网络即可运行整个技术栈。
---
