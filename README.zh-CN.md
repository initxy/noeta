<div align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="docs/assets/logo/noeta-logo-dark.svg">
    <img src="docs/assets/logo/noeta-logo-light.svg" alt="Noeta — 一条事件日志 fold 成状态" width="336">
  </picture>
  <p>
    <a href="https://pypi.org/project/noeta-sdk/"><img alt="PyPI" src="https://img.shields.io/pypi/v/noeta-sdk"></a>
    <a href="https://github.com/initxy/noeta/actions/workflows/ci.yml"><img alt="CI" src="https://github.com/initxy/noeta/actions/workflows/ci.yml/badge.svg?branch=main"></a>
    <a href="https://pypi.org/project/noeta-sdk/"><img alt="Python versions" src="https://img.shields.io/pypi/pyversions/noeta-sdk"></a>
    <a href="LICENSE"><img alt="License: Apache-2.0" src="https://img.shields.io/badge/license-Apache--2.0-blue"></a>
  </p>
</div>

**一直跑下去的 agent。** Noeta 是一个 Python SDK：agent 崩了能接着跑，等人审批等几天也不占资源，
从一个脚本扩到多机集群，agent 代码一行不用改。

[English](README.md) · **简体中文** · [文档站](https://initxy.github.io/noeta/zh/) · [快速上手](https://initxy.github.io/noeta/zh/start/quickstart.html) · [为什么选 Noeta](https://initxy.github.io/noeta/zh/why-noeta.html)

## 几行代码跑起一个 agent

```bash
uv pip install noeta-sdk
export ANTHROPIC_API_KEY=sk-ant-...
```

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

agent 用内置的文件工具看一遍目录，然后回答。`result` 里还有这次运行的每一次模型调用、
工具调用和 token 用量。

## 和别的方案不一样的地方

**崩了能接着跑。** 任务状态不放在内存里，而是从一条只追加的事件日志里重建。worker 跑到一半被杀掉，
另一个 worker 从最后一步接着做。这条日志同时就是完整的审计记录。

**等待不花钱。** 任务可以停下来等人审批、等定时器、等子任务、等外部事件，等几秒或几个月都行。
等的时候不占资源，条件满足时恰好被唤醒一次。

**从脚本到集群。** 脚本里调 `query()`，服务里用 `Client.start_workers(n)` 起 worker 池，
再让几台机器共用一个 Postgres。每一步 agent 代码都一样，也不需要额外部署守护进程或服务。

另外：**一切都是插件**，文件工具、网页、记忆、MCP、沙箱、模型适配器走的都是和你的插件同一套公开接口；
**什么模型都能接**，Anthropic、任何兼容 OpenAI 的网关、OpenAI Responses API，换模型只改一行；
**先审批再动手**，有风险的工具调用会停下来等人批准，guard 能在调用执行前拦下它。

## 和其他方案比

| | **Noeta** | Claude Agent SDK | LangGraph | Temporal |
|---|---|---|---|---|
| 是什么 | 可持久运行的 agent 运行时（一个库） | Claude 的 agent 循环库 | 基于图的 agent 框架 | 持久化工作流平台 |
| 谁决定下一步 | 模型一步步决定 | 模型决定 | 你预先定义的图 | 你写好的工作流代码 |
| 存下来的是什么 | 每一个事件，状态由事件推出来 | 对话记录 | 图状态的检查点 | 工作流历史 |
| 等人 / 等定时器 | 内置，恰好唤醒一次 | 恢复对话 | 中断后由调用方恢复 | 内置 |
| 横向扩展 | worker 池；多机共用 Postgres | 单进程 | 自己想办法，或用托管平台 | Temporal 集群 |
| 模型 | 任意，一行切换 | Claude | 任意 | — |
| 要额外运维的服务 | 没有 | 没有 | 没有 | Temporal 服务端 |

agent 要长时间无人值守地跑，而且要能恢复、能审计、能扩容，就选 Noeta。
[完整对比，以及什么时候不该用它](https://initxy.github.io/noeta/zh/why-noeta.html)。

## 基准测试

| 基准 | 范围 | `noeta-agent` `main`（Claude Opus 4.8） | 榜单情况 |
|---|---|---|---|
| Terminal-Bench 2.1 | 40 题分层抽样 | **82.5%**（33/40） | 公开榜单区间 58.7%–83.8% |
| SWE-bench Verified | 15 题子集 | **86.7%**（13/15） | 最高约 79%，中游约 66–77% |

只用公开 SDK 搭出来的 agent（[noeta-agent](https://github.com/initxy/noeta-agent)），
在官方评测框架（[harbor](https://github.com/harbor-framework/harbor)）上运行，由每道题自带的校验器判分。
两项都是抽样，不是全量榜单成绩。[方法和说明](https://initxy.github.io/noeta/zh/benchmarks.html)。

## 文档

| | |
|---|---|
| **入门** | [快速上手](https://initxy.github.io/noeta/zh/start/quickstart.html) · [教程：搭一个完整的 agent](https://initxy.github.io/noeta/zh/start/tutorial.html) |
| **使用指南** | [接入模型](https://initxy.github.io/noeta/zh/guides/models.html) · [自定义工具](https://initxy.github.io/noeta/zh/guides/tools.html) · [MCP](https://initxy.github.io/noeta/zh/guides/mcp.html) · [子代理](https://initxy.github.io/noeta/zh/guides/subagents.html) · [插件](https://initxy.github.io/noeta/zh/guides/plugins.html) · [测试](https://initxy.github.io/noeta/zh/guides/testing.html) · [部署](https://initxy.github.io/noeta/zh/guides/deploy.html) |
| **原理** | [原理总览](https://initxy.github.io/noeta/zh/how-it-works/) · [架构决策记录（ADR）](docs/adr/) |
| **查阅** | [SDK 参考](https://initxy.github.io/noeta/zh/reference/sdk.html) · [Options](https://initxy.github.io/noeta/zh/reference/options.html) · [内置工具](https://initxy.github.io/noeta/zh/reference/tools.html) |

更想直接看代码？[`examples/`](examples/) 里有可以直接跑的脚本：自定义工具、MCP 服务、权限审批、子代理、
扛过 `kill -9`，每个都带离线测试。[`examples/reference-host/`](examples/reference-host/) 是只用公开接口搭出来的完整宿主程序。

## 许可证

Apache 2.0，见 [`LICENSE`](LICENSE)。
