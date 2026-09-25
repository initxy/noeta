---
layout: home
title: "Noeta — 能扛崩溃、能长时间运行的 Python AI agent SDK"
titleTemplate: false

hero:
  name: Noeta
  text: 一直跑下去的 agent
  tagline: 一个 Python SDK。进程崩了能接着跑，等人审批等几天也不占资源，从一个脚本扩到多机集群，agent 代码一行不用改。
  image:
    src: /logo.svg
    alt: Noeta
  actions:
    - theme: brand
      text: 快速上手
      link: /zh/start/quickstart
    - theme: alt
      text: 为什么选 Noeta
      link: /zh/why-noeta
    - theme: alt
      text: GitHub
      link: https://github.com/initxy/noeta

features:
  - title: 崩了能接着跑
    details: 任务状态不放在内存里，而是从一条只追加的事件日志里重建。worker 跑到一半被杀掉，另一个 worker 从最后一步接着做。这条日志同时就是完整的审计记录。
    link: /zh/how-it-works/event-log
    linkText: 恢复是怎么做的
  - title: 等待不花钱
    details: 任务可以停下来等人审批、等定时器、等子任务、等外部事件，等几秒或者等几个月都行。等的时候不占任何资源，条件满足时恰好被唤醒一次。
    link: /zh/how-it-works/tasks-and-waking
    linkText: 唤醒是怎么做的
  - title: 从脚本到集群
    details: 脚本里调 query()，服务里起一个 worker 池，再让几台机器共用一个 Postgres。每一步 agent 代码都一样，不需要额外部署守护进程或服务。
    link: /zh/guides/deploy
    linkText: 部署上线
---

## 几行代码跑起一个 agent

<p class="nt-lead">装好 <code>noeta-sdk</code>，设好 <code>ANTHROPIC_API_KEY</code>，agent 就能用内置的文件工具去看你的项目。</p>

```python
from noeta.sdk import Options, query
from noeta.sdk.providers import AnthropicProvider

result = query(
    Options(system_prompt="You are a concise coding assistant."),
    goal="What files are in this directory, and what does each one do?",
    provider=AnthropicProvider(),
    model="claude-sonnet-5",
)
print(result.answer())
```

返回的不只是答案：这次运行里每一次模型调用、工具调用和 token 用量都在里面。[快速上手 →](/zh/start/quickstart)

## 整体结构

<NtArchitecture lang="zh" />

agent 走的每一步都追加进事件日志，所以任何一个 worker 都能靠它把任务重建出来。agent 能做的一切——工具、MCP、记忆，连模型本身——都是插件。[原理 →](/zh/how-it-works/)

## 天生可扩展

<div class="nt-cards">
  <div class="nt-card"><strong>一切都是插件</strong><span>内核本身不带任何能力。文件工具、网页、记忆、MCP、沙箱、模型适配器全是插件，走的是和你的插件同一套公开接口。</span></div>
  <div class="nt-card"><strong>什么模型都能接</strong><span>Anthropic、任何兼容 OpenAI 的网关、OpenAI Responses API。换模型只改一行，agent 和它的历史记录都不变。</span></div>
  <div class="nt-card"><strong>先审批，再动手</strong><span>有风险的工具调用会停下来等人批准。guard 可以在调用执行前拦下它；observer 只能旁观，干扰不了任务。</span></div>
</div>

## 和其他方案比

<div class="nt-compare">

| | **Noeta** | Claude Agent SDK | LangGraph | Temporal |
|---|---|---|---|---|
| 是什么 | 可持久运行的 agent 运行时（一个库） | Claude 的 agent 循环库 | 基于图的 agent 框架 | 持久化工作流平台 |
| 谁决定下一步 | 模型一步步决定 | 模型决定 | 你预先定义的图 | 你写好的工作流代码 |
| 存下来的是什么 | 每一个事件，状态由事件推出来 | 对话记录 | 图状态的检查点 | 工作流历史 |
| 等人 / 等定时器 | 内置，恰好唤醒一次 | 恢复对话 | 中断后由调用方恢复 | 内置 |
| 横向扩展 | worker 池；多机共用 Postgres | 单进程 | 自己想办法，或用托管平台 | Temporal 集群 |
| 模型 | 任意，一行切换 | Claude | 任意 | — |
| 要额外运维的服务 | 没有，只有你的进程和数据库 | 没有 | 没有 | Temporal 服务端 |

</div>

<p class="nt-muted">agent 要长时间无人值守地跑，而且要能恢复、能审计、能扩容，就选 Noeta。<a href="why-noeta.html">完整对比 →</a></p>

## 公开榜单上的成绩

<div class="nt-stats">
  <div class="nt-stat"><div class="num">82.5%</div><div class="label">Terminal-Bench 2.1</div><div class="sub">40 题抽样 · 公开榜单 58.7%–83.8%</div></div>
  <div class="nt-stat"><div class="num">86.7%</div><div class="label">SWE-bench Verified</div><div class="sub">15 题子集 · 榜单最高约 79%</div></div>
</div>

<p class="nt-muted">只用公开 SDK 搭出来的 agent（<a href="https://github.com/initxy/noeta-agent">noeta-agent</a> 的 <code>main</code>，Claude Opus 4.8），在官方评测框架上跑出的成绩。两项都是抽样，不是全量榜单成绩。<a href="benchmarks.html">方法和说明 →</a></p>

## 接下来

<div class="nt-cards">
  <a class="nt-card" href="start/quickstart.html"><strong>快速上手</strong><span>五分钟跑起一个真正的 agent。</span></a>
  <a class="nt-card" href="start/tutorial.html"><strong>教程</strong><span>自定义工具、审批、多轮对话、重启不丢的存储。</span></a>
  <a class="nt-card" href="how-it-works/"><strong>原理</strong><span>事件日志、任务与唤醒、插件系统，一页讲一件事。</span></a>
</div>
