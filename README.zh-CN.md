# Noeta

[English](README.md) · **简体中文**

> 一个单机、持久、事件溯源（event-sourced）的 AI agent 运行时。

Noeta 负责托管、记录并调度 agent 的执行——但不规定 agent 该怎么写。agent 走的每
一步都会落入一份只追加（append-only）的 **EventLog**，而一个任务的完整状态由这份
日志 *fold（折叠）* 回来。挂起与恢复、崩溃恢复、replay、exactly-once wake 都不是额
外加上去的功能；它们是"把日志当作唯一事实来源"这一前提的自然结果。

如果说一个 in-process 的 agent 库（claude-agent-sdk、LangChain）给你的是那个执行
循环，那么 Noeta 在它下面补上了持久化的底座——agent 的历史是一份你可以 fold、检
查、重新进入的日志，而不是随进程消失的临时内存。

<p align="center">
  <img src="docs/assets/web-app.png" alt="Noeta coding-agent web 应用" width="820">
  <br>
  <em>由 <code>python -m noeta.agent</code> 启动的内置 coding-agent web 应用。</em>
</p>

<p align="center">
  <img src="docs/assets/trace.png" alt="Noeta 任务级 trace 视图" width="820">
  <br>
  <em>任务级 trace 视图——每个事件、每轮 LLM 调用、token/cache 统计，全部直接来自 EventLog。</em>
</p>

## 为什么用 Noeta

- **天生持久** —— 每一次状态变化都是一条追加事件；任务状态始终由日志确定性地
  fold 出来，从不跨运行常驻内存。任务跑到一半杀掉进程，fold 能把它原样带回来。
- **provider 中立** —— Anthropic 与 OpenAI 兼容端点都是同一套内部 protocol 背后的
  adapter。换 provider 是接线，不是重写；没有任何厂商的形状会渗进内核。
- **自带你自己的 agent** —— 运行时负责托管与调度，policy、tools、context 由你提
  供。仓库内置了一个 ReAct policy 和一个 coding agent，但没有任何地方强制你用它们。
- **离线优先** —— 一个确定性的 `stub` provider 无需 API key、无需网络就能跑通整个
  栈，因此在一次全新 checkout（以及 CI）上就能验证安装、存储与接线是否正确。
- **按需选层** —— 你可以内嵌内核、引入 SDK，或者直接运行开箱即用、自带 web UI 的
  coding agent。

## 快速开始（无需 API key）

`stub` provider 是一个确定性的两轮 LLM 替身——无 key、无网络。用它就能端到端验证
安装 + 存储 + engine 接线。

```bash
# 安装 coding agent，会顺带把 SDK + runtime 一起装上。
pip install noeta-agent
python -m noeta.agent   # 启动离线 stub coding agent + 内置 web
```

### 一条命令（Makefile）

仓库根目录的 `Makefile` 把"构建 web 应用"和"启动 backend"串了起来，省得你自己记。
入口仍然是 `python -m noeta.agent`；Makefile 只是构建前端，并把几个便捷开关映射到
已有的 `NOETA_AGENT_*` 环境变量上。

```bash
make install   # 首次：可编辑安装 + web 依赖
make run        # 构建 web + 启动 backend（离线 stub，端口 8765）
#  → 打开 http://127.0.0.1:8765/chat

make run PORT=9000   # 覆盖某个开关；`make dev` 会跑 vite 热更新那一对进程
```

### 接入真实模型

复制模板、填入你的 key，`make run` 会自动读取。key 存在一个被 gitignore 的文件
里，永远不会被提交。

```bash
cp noeta.config.example.json noeta.config.json
make run                                              # 默认读取 ./noeta.config.json
make run CONFIG=examples/openai-compatible/config.json   # 或指向任意 JSON 配置
```

### 把启动写成一段 Python 程序

同样的启动过程就是一小段程序——构建离线 backend、验证它能服务、再关掉：

```python
from noeta.agent.backend.lifecycle import BackendConfig, serve_backend

# 默认完全离线：两轮 stub provider，:memory: 存储。
# port=0 绑定一个由操作系统分配的端口。工作区就是当前目录。
config = BackendConfig(port=0)
server, url, shutdown = serve_backend(config)
try:
    assert url.startswith("http://")
finally:
    shutdown()
```

backend 会绑定一个临时端口，并在一秒内把内置 web 应用服务起来。

## 三个发行物

Noeta 以"两个库 + 一个应用外壳"的形式发布。装你需要的最上层那个即可——每一个都会
把它下面的层一起拉进来。

| 发行物 | 位置 | 是什么 |
| --- | --- | --- |
| **`noeta-runtime`** | `packages/noeta-runtime` | engine：事件溯源内核（Engine、fold、snapshot、Worker/Dispatcher、storage、guards、observers）**加上**跑在其上的 agent 素材——ReAct policy，fs/shell/mcp tools，Anthropic + OpenAI 兼容 provider，context composer + skills，以及官方 preset agents。在进程内跑一个 agent 所需的一切。 |
| **`noeta-sdk`** | `packages/noeta-sdk` | engine 之上一层很薄的 in-process 客户端表面：`import noeta.sdk`，然后 `query` / `Client` / `Options` / `tool`。不暴露 engine 内部，不涉及 HTTP。对标 claude-agent-sdk / LangChain。 |
| **`noeta-agent`** | `apps/noeta-agent` | 官方的、以工作区为作用域的 **coding-agent 应用**，构建在 SDK 之上：一个 HTTP/SSE backend、内置 web 应用（`apps/web`）、slash 命令与内置 skills。`python -m noeta.agent` 是唯一入口。 |

没有 `noeta` 命令行脚本——coding agent 及其 web UI 用 `python -m noeta.agent`
启动。

## 自己写应用（SDK）

不想用内置的 coding agent？`import noeta.sdk`，自己来驱动——定义 tool、指定一个工作
区、跑一个模型，再读回那份持久的事件流。没有应用外壳，也没有 HTTP。就像
claude-agent-sdk / LangChain，只不过每一轮都落进运行时赖以构建的、可 fold 的那份
EventLog。

```python
from pathlib import Path

from noeta.sdk import query, Options, tool, ToolContext, ToolResult
from noeta.sdk.providers import AnthropicProvider

# 一个 tool 就是返回 ToolResult 的函数。`version` 是 tool 身份的一部分，因此必填。
@tool(name="word_count", version="1", risk_level="low", input_schema={
    "type": "object",
    "properties": {"text": {"type": "string"}},
    "required": ["text"],
})
def word_count(arguments: dict, ctx: ToolContext) -> ToolResult:
    n = len(str(arguments["text"]).split())
    return ToolResult(success=True, output=f"{n} words")

options = Options(
    system_prompt="You are a concise assistant.",
    name="main",
    allowed_tools=("read", word_count),   # 内置 tool 用名字，自定义 tool 用对象
    permission_mode="bypassPermissions",
)

# query() 驱动一轮，返回完整的事件信封（event-envelope）流——agent 所做一切的、
# 机器可读的记录。
for env in query(
    options,
    goal="How many words are in 'the quick brown fox'?",
    provider=AnthropicProvider(api_key="sk-ant-...", default_max_tokens=1024),
    workspace_dir=Path("."),
    model="claude-sonnet-4-5",   # 换成你的 provider 支持的任意 model id
):
    print(env.type)
```

需要多轮会话时，用 `Client` 而不是 `query`（`client.start(...)`，再用
`client.messages(task_id)` 拿到人类可读的投影）。这一切**离线、无需 API key** 也能
跑——把 provider 换成 `noeta.testing` 里那个脚本化的 `FakeLLMProvider` 即可。可端到端
运行的完整示例：

- [`examples/sdk_minimal.py`](examples/sdk_minimal.py) —— 纯 SDK 路径，离线
- [`examples/custom_tool.py`](examples/custom_tool.py) —— 自定义 `@tool`
- [`examples/swap_provider.py`](examples/swap_provider.py) —— Anthropic ↔ OpenAI 兼容
- [`examples/spawn_subtask.py`](examples/spawn_subtask.py) —— 委派给 sub-agent

## Noeta 对比 Claude Agent SDK —— 从服务端视角看

两者都提供 agent loop、tools、MCP 和 sub-agent。差别在**底下那根脊梁**,而这根脊
梁在 agent 跑在服务端时最要紧——长时运行、能扛重启、可审计。Claude Agent SDK 是一
个轻量的 in-process 客户端,驱动一个"托管得很好的" agent;Noeta 则是一套你自己拥
有的、持久的、可自托管的执行底座。(Anthropic *完全托管* 的服务端选项是另一个产品
——Managed Agents,由 Anthropic 跑 loop 和 sandbox;那是用"放弃对底座的所有权"换
"零运维"。)

| 服务端关注点 | Claude Agent SDK | Noeta |
| --- | --- | --- |
| 谁拥有执行底座 | 你托管 loop,状态活在客户端进程里 | 你托管它;loop、日志、调度器都跑在**你自己的**基础设施上 |
| 状态 / 恢复 | session JSONL(一段对话录音);resume 靠回放对话 | `state = fold(events)`;崩溃恢复就是一次重折叠,没有单独的加载路径 |
| 挂起 / 恢复 / exactly-once wake | 用 session id 做 resume / fork | 一等公民:durable wake 能扛住 worker 崩溃(目前单机) |
| 上下文压缩 | 自动摘要,不可逆(要存档得自己用 PreCompact hook 抓一份) | 是一条被记录、可回放的事件;原始历史从不被抹掉 |
| provider | 可配多个后端,但形状是 Anthropic 为中心的 | 厂商中立的内部 protocol;内核不允许依赖任何厂商 |
| 调度 / 分布式 | 单个 in-process query / client | lease + 持久日志的队列底座(当前单机 / 单 worker) |

**各自何时更合适。** 想要**开箱即用、紧跟官方 Claude 能力**——运维负担和生态都归
Anthropic——选 **Claude Agent SDK**。想让 agent 的执行变成一份**你自己拥有、可回放
的、可审计的、厂商中立的账本**——代价是这套底座得你自己跑、自己运维——选 **Noeta**。

**服务端的诚实警告(Noeta)。** 它还是 pre-1.0,而且只交付了**单机 / 单 worker**
——生产集群需要多机,目前意味着换掉 storage adapter 并搭起一个 worker pool(引擎不
变,但这部分工作还没交付)。生态更小、内置集成更少——你要自己维护的东西更多,也没
有托管服务或厂商 SLA。

## 文档

- [`docs/quickstart.md`](docs/quickstart.md) —— 离线冒烟 + 真实 provider 走一遍
- [`docs/concepts.md`](docs/concepts.md) —— 核心模型：Task / EventLog / Dispatcher / Engine / Guard / Observer / Policy / Composer
- [`docs/noeta-agent.md`](docs/noeta-agent.md) —— `python -m noeta.agent` coding agent：tools、presets、skills、write/shell policy、HTTP 表面、MCP / hooks
- [`docs/noeta-architecture-deep-dive.md`](docs/noeta-architecture-deep-dive.md) —— 自顶向下的架构讲解，并与 Claude Agent SDK 对比
- [`docs/failure-modes.md`](docs/failure-modes.md) —— 缺 API key、预算耗尽、durable exactly-once wake 恢复
- [`docs/adr/`](docs/adr/) —— 架构决策记录（ADR）：每一个跨模块决策*为什么*是现在这样（受众：任何即将改这段代码的人）。术语表见 [`CONTEXT.md`](CONTEXT.md)。

SDK 用法——最小 agent、自定义 tool、切换 provider、委派给 sub-agent——见
[`examples/`](examples/)。

## 安装

Noeta 已发布到 PyPI。各发行物按依赖串联（`noeta-agent` → `noeta-sdk` →
`noeta-runtime`），装最上层的包会把其余层一起拉进来。需要 Python 3.11 或更高版本。

```bash
# 默认的 coding-agent 体验（会拉 noeta-sdk + noeta-runtime）
pip install noeta-agent

# 只要 SDK —— 自己写、自己托管 agent（会拉 noeta-runtime）
pip install noeta-sdk

# 只要内核 —— 内嵌 runtime
pip install noeta-runtime
```

做开发时改用从 checkout 的可编辑安装——每个 `-e` 包都会把它在 workspace 里的
同层一起拉进来：

```bash
uv pip install -e apps/noeta-agent   # 或 packages/noeta-sdk、packages/noeta-runtime

# 直接从 git 安装，无需 checkout
pip install "noeta-agent @ git+https://github.com/initxy/noeta.git#subdirectory=apps/noeta-agent"
```

## 仓库结构

所有发行物都向共享的 PEP 420 `noeta.` 命名空间贡献子包（不存在顶层
`noeta/__init__.py`）。

```
packages/
  noeta-runtime/     # engine = 内核（机制）+ agent 素材
    noeta/
      protocols/    # dataclass + Protocol 类型 —— 唯一有类型约束的边界
      core/         # Engine / fold / snapshot / HookManager
      runtime/      # Worker / Dispatcher / ToolRuntime / RuntimeLLMClient / Compaction
      storage/      # InMemory + Sqlite adapter
      guards/       # BudgetGuard / PermissionGuard
      observers/    # Audit / Metrics / SSE fanout
      read_models/  # 只读投影
      policies/     # ReActPolicy / stub policy
      tools/        # fs / shell / mcp / fake
      providers/    # Anthropic + OpenAI 兼容 adapter
      context/      # ThreeSegmentComposer / skill registry
      execution/    # 通用 driver / runner / resolver / builder
      agent/        # AgentSpec / AgentRegistry / 确定性 fingerprint
      presets/      # 官方 4 个 agent：main / explore / plan / general-purpose
      testing/      # 仓库内测试替身 / harness 辅助
  noeta-sdk/         # 薄薄一层 in-process 客户端表面（不暴露 engine 内部，无 HTTP）
    noeta/
      sdk/          # 公共 facade：query / Client / Options / tool / 扩展接口
      client/       # engine 之上的 HostConfig / Options 编译
apps/
  noeta-agent/        # 官方 coding-agent 应用外壳
    noeta/
      agent/         # backend (HTTP/SSE) / host builder / commands / __main__ / 内置 skills
                     #   wheel 会把 apps/web force-include 到 noeta/agent/static
  web/               # 独立 coding SPA（仅 HTTP/SSE 客户端）
tests/              # pytest 套件（仓库根）
docs/               # 面向用户的文档
docs/adr/           # 架构决策记录
examples/           # SDK 用法示例（+ _internal/ 内核 demo）
scripts/            # lint 脚本（命名 + engine 行数预算）
```

## 开发

```bash
uv sync
uv run pytest
uv run lint-imports --config .importlinter
uv run python scripts/lint-naming.py
```

无论是人还是 agent，贡献都从根目录的 [`AGENTS.md`](AGENTS.md) 路由开始；
[`CONTRIBUTING.md`](CONTRIBUTING.md) 会把你指向它以及各项协作约定。

## 状态与范围

Noeta 处于早期、pre-1.0 的预览阶段。它能跑、有测试、内核也稳定，但有些能力现阶段
是有意不在范围内的：

- **仅单机。** 现有 worker 在进程内把 dispatcher 抽干，这是预览版而非生产级
  daemon。多机协调不在讨论范围。
- **持久 wake 仅限单机 / 单 worker。** single-worker durable exactly-once wake 已
  经交付；多 worker 并发 + fencing，以及 partial-step-orphan 边界（一步执行到一半崩
  溃）仍是限制——见 [`docs/failure-modes.md`](docs/failure-modes.md)。
- **human-in-the-loop / timer wake** —— engine 已经带了形状，完整的 UX 还在落地。
- **前端** —— 现有 web 应用是一个用原生 ES modules 写的小型 Vite MPA；预览阶段不
  计划迁移框架。

## 许可协议

Apache License 2.0 —— 见 [`LICENSE`](LICENSE)。
