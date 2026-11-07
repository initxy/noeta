<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="https://raw.githubusercontent.com/initxy/noeta/main/docs/assets/logo/noeta-logo-dark.svg">
    <img alt="noeta" src="https://raw.githubusercontent.com/initxy/noeta/main/docs/assets/logo/noeta-logo-light.svg" height="88">
  </picture>
</p>

<h3 align="center">兼容 Claude Code、可以自己部署成多机集群的编程智能体。</h3>

<p align="center">
  兼容 Claude Code · 一个 Postgres 配任意多个工人 · 哪个工人都能接着跑任何任务 · 等人回复不占进程
</p>

<p align="center">
  <a href="https://pypi.org/project/noeta/"><img alt="PyPI" src="https://img.shields.io/pypi/v/noeta?color=10b981"></a>
  <a href="https://pypi.org/project/noeta/"><img alt="Python" src="https://img.shields.io/pypi/pyversions/noeta"></a>
  <a href="https://github.com/initxy/noeta/actions/workflows/ci.yml"><img alt="CI" src="https://github.com/initxy/noeta/actions/workflows/ci.yml/badge.svg"></a>
  <a href="https://github.com/initxy/noeta/blob/main/LICENSE"><img alt="License" src="https://img.shields.io/badge/license-Apache--2.0-blue"></a>
  <a href="https://initxy.github.io/noeta/zh/"><img alt="文档" src="https://img.shields.io/badge/文档-initxy.github.io%2Fnoeta-8b5cf6"></a>
</p>

<p align="center">
  <a href="https://initxy.github.io/noeta/zh/">文档</a> ·
  <a href="https://initxy.github.io/noeta/zh/start/quickstart">快速上手</a> ·
  <a href="https://initxy.github.io/noeta/zh/how-it-works/">设计与原理</a> ·
  <a href="https://initxy.github.io/noeta/zh/benchmarks">跑分</a> ·
  <a href="https://github.com/initxy/noeta/blob/main/README.md">English</a>
</p>

---

**noeta** 是一个编程智能体：在模型看来它就是 Claude Code，在你看来它是一个服务端程序。
模型拿到的工具、提示词规则、提醒和请求结构都和 Claude Code 一样，所以模型在 Claude Code 上练出来的
习惯原样有效，你现有的 Claude Code 设置、hooks、技能、子代理、MCP 服务器和插件也都能用。底层每个
任务都是存储里的一条日志：让任意多个工人连同一个 Postgres，哪个工人都能接着跑任何任务；等人回复的
任务一个进程都不占。单机用 SQLite 也照样能跑，可以在终端里直接用，也可以嵌进 Python 程序。

<p align="center">
  <img alt="noeta 的终端界面在修一个失败的测试" src="https://raw.githubusercontent.com/initxy/noeta/main/tui/screenshots/transcript.svg" width="760">
</p>

## 亮点

- 🌐 **自己部署，工人想加几个加几个。** 一个 Postgres 存储，任意多个 `noeta worker` 进程，前面一个
  `noeta serve`。任务不归哪个工人所有：每一步都是先认领、再执行、再追加到任务日志里，所以下一步在
  哪个工人上跑都行。发版重启、进程崩溃、整台机器没了，任务只是换个工人接着跑；当时正在执行的工具
  调用会如实告诉模型「结果未知」，绝不会悄悄再跑一遍。
- ⏸️ **等待不占进程。** 等审批、问人问题、等子代理、等定时器，任务都会挂起：它只是存储里的一行记录
  和一条日志，背后没有进程也没有工人，几分钟还是几周后回复到了再醒。
- 🧠 **兼容 Claude Code，而且有核对。** 工具名和参数、提示词规则、提醒、上下文压缩都跟着 Claude Code
  走。`make parity` 会离线重放录下来的 Claude Code 真实会话，把 noeta 发出的每个请求和 Claude Code
  的逐个比对；有意保留的差异都单独列出并写明原因。设置、hooks、技能、子代理、MCP、插件目录都用
  Claude Code 的格式。
- 💻 **单机也能用。** 同一套代码在一个进程里用 SQLite 就能跑：终端界面、`noeta -p`，或者当作库嵌进
  你的 Python 程序。
- 🔌 **怎么接都行。** 全屏终端界面、Claude Code 的 `-p` 和 `stream-json` 协议（Agent SDK 可以直接
  接上）、给 Zed 等编辑器用的 ACP、HTTP / WebSocket API、Python SDK；另有网页版和桌面版，都还在
  实验阶段，需要从源码构建。
- 🤖 **模型随便换。** Claude、DeepSeek，或者任何 OpenAI 兼容 / Responses 接口，包括你自己部署的
  vLLM 或网关。
- 🧩 **一切都是插件。** 文件、shell、权限、hooks、技能、子代理、MCP、记忆、压缩，全都实现同一个
  `Extension` 接口，内置插件和你写的插件加载方式完全一样。
- 🔒 **不往外发任何东西。** 没有遥测、不上传日志、不检查更新。除了你配置的模型服务商，
  只有模型按你的权限规则调用工具时才会联网。

## 跑分

两边用的是同一个模型 DeepSeek V4.1 Flash（effort `high`），不是 Claude 模型；同一批任务、同样的
限制、同一台机器：

| 测试集 | noeta | Claude Code | 差值（标准误） |
|---|---|---|---|
| SWE-rebench 07-2026（111 题，两轮合并） | 50.5 % | 45.0 % | +5.4（3.6） |
| 跑完 111 题一轮的花费 | $5.01–5.77 | $5.70–6.25 | |

结论是持平或略好：差值大约是 1.5 个标准误，不算显著，单跑一轮本身就会上下浮动 5 个百分点左右。
每一轮的结果、重试情况、答案泄露的题目，见[完整跑分报告](https://initxy.github.io/noeta/zh/benchmarks)。

## 快速上手

需要 Python 3.11 以上。

```bash
uv tool install 'noeta[tui]'          # 或者：pip install 'noeta[tui]'

export ANTHROPIC_API_KEY=sk-ant-...   # 用别的模型见下面
export NOETA_MODEL=claude-sonnet-5-5

noeta                                 # 在当前目录打开全屏终端界面
noeta -p "这个仓库是做什么的？"         # 问一句，打印回答
```

<details>
<summary><b>用别的模型</b>（DeepSeek、OpenAI 兼容接口、自建网关）</summary>

```bash
export NOETA_PROVIDER=openai_compat NOETA_BASE_URL=https://api.deepseek.com/v1
export NOETA_MODEL=deepseek-chat NOETA_API_KEY=...
```

`openai_compat` 的地址以 `/v1` 结尾；`anthropic` 只写到域名；`openai_responses` 写完整的
`…/v1/responses`。这些设置也可以用命令行参数（`--model`、`--provider`、`--base-url`）或者写进
`~/.noeta/config.toml`。详见[模型与服务商](https://initxy.github.io/noeta/zh/guides/models)。

</details>

<details>
<summary><b>日常用法</b></summary>

- `noeta -c` 接着这个目录里最近的一次对话；`noeta -r` 列出所有对话。
- 项目说明读 `NOETA.md`，没有就读 `AGENTS.md`。
- 技能从项目里和家目录下的 `.noeta/skills`、`.agents/skills` 加载。
- 项目自己的 `.noeta/` 设置、子代理、技能和插件，要先 `noeta trust` 才会加载。
- `noeta -p --output-format stream-json --input-format stream-json --verbose` 说的是 Claude Code
  的 SDK 协议，Claude Agent SDK 可以直接驱动它。

</details>

<details>
<summary><b>可选组件</b></summary>

| 组件 | 提供 |
|---|---|
| `tui` | 终端界面（`noeta-tui`，和 noeta 同版本发布） |
| `acp` | `noeta acp`：给 Zed 等编辑器用的 Agent Client Protocol |
| `server` | `noeta serve`：HTTP / WebSocket API |
| `postgres` | Postgres 存储，多工人部署用 |
| `keyring` | 插件设置里用 `{keyring:service/user}` 引用密钥 |

一次装几个：`uv tool install 'noeta[tui,server,postgres]'`。

</details>

## 部署到多台机器

一个 Postgres、任意多个工人、前面一个 HTTP API：

```bash
export NOETA_STORE=postgres://noeta:...@db.internal/noeta
noeta worker --concurrency 16             # 每台工人机器上跑
noeta serve --no-worker --host 0.0.0.0    # API（需要 bearer token）
noeta --server http://api.internal:8642   # 终端界面连过去
```

工人挂了什么也不会留下：别的工人从日志停下的地方接手。详见[作为服务端运行](https://initxy.github.io/noeta/zh/guides/server)。
想先在一台 Docker 主机上试试，[`deploy/`](https://github.com/initxy/noeta/tree/main/deploy) 里有
Dockerfile 和一份 compose 文件（Postgres、`noeta serve` 加三个工人），说明见
[用 Docker 部署](https://initxy.github.io/noeta/zh/guides/docker)。

## 嵌进 Python

```python
import asyncio
import noeta
from noeta.core.events import ModelReplied

async def main() -> None:
    store = noeta.store("sqlite:///tasks.db")
    agents = {a.name: a for a in noeta.default_agents("claude-sonnet-5-5")}
    model = noeta.provider("anthropic", api_key="sk-ant-...")
    runtime = noeta.Runtime(store=store, agents=agents, provider_for=lambda _name: model)
    client = noeta.Client(store)
    task = await client.create("main", "What does this repository do?", config={"cwd": "."})
    await runtime.run_until_idle()
    replies = [r.event for r in await client.events(task) if isinstance(r.event, ModelReplied)]
    print(replies[-1].response.message.text)

asyncio.run(main())
```

`noeta.LocalApp` 把存储、工人和客户端放在一个进程里；`noeta.connect(url)` 连一个存储地址或者
`noeta serve` 服务。详见[嵌入到程序里](https://initxy.github.io/noeta/zh/start/embedding)。

## 扩展

用 Python 写插件，通过 entry point 发布，或者直接丢一个文件到 `~/.noeta/plugins/`。子代理是
Claude Code 格式的 markdown 文件，提示词都有名字、可以覆盖，Claude Code 的插件目录原样加载。
[写插件](https://initxy.github.io/noeta/zh/guides/plugins) ·
[和 Claude Code、pi、DeepSeek Harness 的对比](https://initxy.github.io/noeta/zh/operations/comparison)

## 原理

```
 终端界面 · -p / stream-json · ACP · HTTP API · 网页 · 桌面 · Python
                              │  同一套客户端 API
                              ▼
                ┌─────────────────────────────┐
                │  存储：每个任务一条日志        │   SQLite 或 Postgres
                └─────────────────────────────┘
                   ▲ 认领 · 重放 · 走一步 · 追加
        工人  工人  工人 …   （模型看到的内容由插件拼出来）
```

工人认领一个任务，把日志重放成当前状态，调一次模型，跑完工具，再把发生的事追加回日志。每次写入
都带着它预期的位置和手里的认领凭证，所以一个卡住、丢了认领的工人永远写不进去。
[设计与原理](https://initxy.github.io/noeta/zh/how-it-works/)讲了每个部分怎么做、为什么这么做。

## 参与贡献

欢迎提 issue 和 PR。先看 [CONTRIBUTING.md](https://github.com/initxy/noeta/blob/main/CONTRIBUTING.md)；
术语表在 [CONTEXT.md](https://github.com/initxy/noeta/blob/main/CONTEXT.md)，设计决策在
[docs/adr/](https://github.com/initxy/noeta/blob/main/docs/adr/index.md)。安全问题见
[SECURITY.md](https://github.com/initxy/noeta/blob/main/SECURITY.md)，变更记录见
[CHANGELOG.md](https://github.com/initxy/noeta/blob/main/CHANGELOG.md)。

## 许可证

[Apache-2.0](https://github.com/initxy/noeta/blob/main/LICENSE)
