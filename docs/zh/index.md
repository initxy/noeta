---
layout: home
title: "noeta：兼容 Claude Code 的开源编程智能体"
titleTemplate: false

hero:
  name: noeta
  text: 兼容 Claude Code、可以自己部署成多机集群的编程智能体。
  tagline: 模型看到的和 Claude Code 给它的一样。底下每个任务都是 Postgres（或 SQLite）存储里一条只追加、只有一个写入者的日志：哪个 worker 都能接着跑任何任务，等人回复的任务一个进程都不占，进程崩溃或发版重启也只是让任务换一个 worker 接着跑。
  image:
    src: /logo.svg
    alt: noeta
  actions:
    - theme: brand
      text: 快速上手
      link: /zh/start/quickstart
    - theme: alt
      text: 设计与原理
      link: /zh/how-it-works/
    - theme: alt
      text: GitHub
      link: https://github.com/initxy/noeta

features:
  - title: 很多 worker，一个存储
    details: 任意多个 worker 连同一个 Postgres，前面挂一个 HTTP 接口。任务不归哪个 worker 所有，每一步都可以在任何一台上跑。笔记本上用 sqlite，跑的是同一套代码。
    link: /zh/guides/server
    linkText: 作为服务端运行
  - title: 对模型而言，就是 Claude Code
    details: 工具名和参数格式、提示词规则、提醒、压缩方式、请求结构都和 Claude Code 一致。设置、hooks、技能、代理、MCP 服务器、插件目录也用 Claude Code 的格式，只是放在 noeta 自己的目录里。
    link: /zh/how-it-works/parity
    linkText: 怎么对齐 Claude Code
  - title: 任务就是一条日志
    details: 十种事件，每个任务同一时刻只有一个写入者，用 epoch 隔开过期的写入。状态全靠重放日志得出，所以任务崩了能在任何一台 worker 上接着跑，工具不会被稀里糊涂地重跑一遍。
    link: /zh/how-it-works/event-log
    linkText: 事件日志
  - title: 等待不占进程
    details: 问人一个问题、等一次审批、等子代理、等定时器，任务都会挂起：不占 worker、不占进程，直到回复进了它的收件箱，哪怕是几周以后。
    link: /zh/how-it-works/waiting
    linkText: 等待与唤醒
  - title: 一切都是插件
    details: 文件、shell、权限、hooks、技能、子代理、MCP、记忆、压缩，全都实现同一个接口 Extension。内置插件和你写的插件加载方式一模一样。
    link: /zh/how-it-works/plugins
    linkText: 插件体系
  - title: 各种入口
    details: 终端界面、Claude Code 的 -p 和 stream-json 协议、给编辑器用的 ACP、HTTP / WebSocket 接口、Python SDK，还有实验中的网页版。底下都是同一个客户端接口。
    link: /zh/reference/cli
    linkText: noeta 命令
---

## 整体结构

<Diagram name="home-overview" />

所有前端都调用同一个客户端接口：创建任务、发消息、回答、取消、读事件、跟踪事件。worker
从存储里认领任务，重放这个任务的日志得到当前状态，调用模型，通过插件执行工具，再把发生的事追加
进日志。模型看到的内容由插件拼出来，和 Claude Code 保持一致。[设计与原理 →](./how-it-works/index.md)

## 和别的 agent 有什么不同

**很多 worker，一个存储。** 让任意多个 `noeta worker` 进程连同一个 Postgres 存储，前面放一个
`noeta serve`。worker 每次只认领任务的一步，做完就把发生的事追加进任务日志，所以下一步在哪个
worker 上跑都行：发版、崩溃、整台机器没了，任务只是换个地方接着跑。前端连哪台服务端都是同一套接口
（[作为服务端运行](./guides/server.md)、[用 Docker 部署](./guides/docker.md)）。

**在服务器上兑现 Claude Code 的约定。** 模型是照着 Claude Code 的工具和提示词调出来的，所以
noeta 照搬的是模型看到的东西，而不是 Claude Code 的内部实现：工具名和参数格式、提示词各部分的
顺序和角色、提醒、压缩、发给 provider 的请求。`make parity` 会离线重放录下来的 Claude Code 真实会话，
把 noeta 发出的每个请求逐一比对，凡是没有列为「有意保留、写明原因」的差异都会报出来
（[如何对齐 Claude Code](./how-it-works/parity.md)）。你的设置、
hooks、技能、代理、MCP 服务器和插件目录保持 Claude Code 的格式，但放在 `~/.noeta` 和 `.noeta/`
里；noeta 从不读 `~/.claude`（[配置](./start/configuration.md)）。

**只有一个写入者的事件日志。** 一个任务就是存储里的一串事件，一共十种。worker 写之前必须先认领
任务，每次追加都要带上它预期的序号和这次认领的 epoch，所以一个卡住后丢了认领的 worker 再也写不
进去。工具调用在执行之前先记一笔"开始了"；如果中途崩溃，下一个 worker 会把没跑完的调用答成"结果
未知"，而不是再跑一遍（[任务就是一条日志](./how-it-works/event-log.md)、
[单写者与崩溃恢复](./how-it-works/recovery.md)）。

**等待不占进程。** 权限确认、`AskUserQuestion`、子代理、定时器，都会让任务挂起：记一条
`Waiting`，交还认领，从所有 worker 上消失，直到收件箱里来了回复。一个在等人的任务只是存储里的
一行和一条日志，背后没有线程，也没有进程（[等待与唤醒](./how-it-works/waiting.md)）。

**一切都是插件。** 内核只负责跑日志；所有能力都是插件，实现同一个接口 `Extension`，通过 Python
entry point 按名字找到，也可以是 `~/.noeta/plugins/` 里的单个文件。任务会记下它启动时用了哪些插件，
缺了某个插件的 worker 绝不会在少了它的情况下跑这个任务（[插件体系](./how-it-works/plugins.md)、
[写一个插件](./guides/plugins.md)）。

**默认不外发任何东西。** 除了发给你配置的模型 provider 的请求，noeta 不往任何地方发数据：没有遥测，
不上传日志，不检查更新。只有模型调用工具时（在你的权限规则之下），工具才会去联网。

## 和 Claude Code 同场测评

<div class="nt-stats">
  <div class="nt-stat"><div class="num">+5.4</div><div class="label">SWE-rebench 07-2026：noeta 减 Claude Code，标准误 3.6</div><div class="sub">111 题，每题限时 50 分钟，两轮合并 · noeta 50.5 %，Claude Code 2.1.312 45.0 % · 持平或略好，不算显著</div></div>
  <div class="nt-stat"><div class="num">+1.9</div><div class="label">Terminal-Bench 4.0：noeta 减 Claude Code，标准误 3.2</div><div class="sub">66 题中的 54 题，限时 2 小时，一轮 · noeta 3.7 %，Claude Code 1.9 % · 这个模型上两边都接近零</div></div>
</div>

<p class="nt-muted">noeta 1.0 和 Claude Code 2.1.312 用同一个模型 DeepSeek V4.1 Flash（effort
<code>high</code>），不是 Claude 模型；同样的题目、限时和机器。每轮花费在同一个区间（SWE-rebench 上
noeta $5.01–$5.77，Claude Code $5.70–$6.25）。
<a href="./benchmarks.html">方法、每一轮的数据和注意事项 →</a></p>

## 接下来看什么

<div class="nt-cards">
  <a class="nt-card" href="./start/quickstart.html"><strong>快速上手</strong><span>安装、选模型，五分钟内跑起终端界面或 <code>noeta -p</code>。</span></a>
  <a class="nt-card" href="./start/embedding.html"><strong>在 Python 里嵌入</strong><span>在你自己的程序里用存储、Runtime 和客户端。</span></a>
  <a class="nt-card" href="./guides/server.html"><strong>作为服务端运行</strong><span>一个 Postgres 存储，多台 worker，前面挂 HTTP 接口；有现成的 Docker compose 文件可以起步。</span></a>
  <a class="nt-card" href="./why-noeta.html"><strong>为什么是 noeta</strong><span>它适合做什么、适合谁、不打算做什么。</span></a>
</div>
