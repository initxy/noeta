# 为什么选 Noeta

大多数 agent 库给你的是一个在单个进程里跑的循环。做个聊天窗口够用了；可一旦
agent 要无人值守地跑几个小时、要停下来等人、或者一台机器不够用，这种循环就撑不住。
Noeta 就是为这些场景做的。

## 崩溃只是暂停，不会丢东西

Noeta 从不把任务状态放在内存里。每一次模型调用、工具调用和决策都追加进一条事件日志，
需要状态时再从日志重建。进程跑到一半挂了也不丢任何东西：下一个 worker 重放日志，
从最后记录的那一步接着做。做到一半被打断的那一步，能安全重跑就重跑；如果重跑可能把需要审批的操作再做一遍，
任务就停下来等人处理。

<NtCrashResume lang="zh" />

```python
from noeta.sdk import Client, HostConfig, Options
from noeta.sdk.providers import AnthropicProvider

options = Options(system_prompt="You are a careful release engineer.")
db = HostConfig(storage_path="./noeta.sqlite")   # 也可以是 postgresql:// 连接串

with Client(options, provider=AnthropicProvider(), model="claude-sonnet-5",
            host_config=db) as client:
    task_id = client.start(goal="Draft the release notes.").task_id

# 重启之后，另一个进程能把同一个任务读回来
with Client(options, provider=AnthropicProvider(), model="claude-sonnet-5",
            host_config=db) as client:
    for item in client.messages(task_id):
        print(item)
```

事后想知道「agent 做了什么、为什么这么做」，也看这条日志：每次工具调用、审批、
token 用量、上下文压缩都记在上面，而且什么都不会被覆盖。
→ [事件日志与故障恢复](how-it-works/event-log.md)

## 等待是一种状态，不是一个卡住的线程

任务可以停下来等：

- 人批准一次有风险的工具调用，或者回答一个问题
- 定时器到点
- 它派出去的子任务做完
- 外部事件（webhook、CI 结果）

<NtWaiting lang="zh" />

等的时候什么都不跑，内存里也不留东西。等的东西一到，恰好有一个 worker 把它唤醒，
而且只唤醒一次，中间崩了也一样。等一个月的审批和等五秒的工具调用走的是同一套机制。

```python
turn = client.start(goal="Clean up the build directory.")
if turn.status == "suspended":          # 比如在等人批准一条 Bash 命令
    print(turn.wake_handle)             # 它在等什么
    # ……几小时后，甚至换一个进程：
    client.approve(turn.task_id, call_id="...")
```

→ [任务与唤醒](how-it-works/tasks-and-waking.md)

## 规模变了，代码不用重写

agent 就是一个 `Options` 值。怎么运行它是另一回事，换运行方式不用动 agent。

<NtScale lang="zh" />

| 阶段 | 要改的地方 |
|---|---|
| 一个脚本 | `query(options, goal=...)`：调一次，拿一个答案 |
| 一个服务 | `Client(options, ...)` 加上 `client.start_workers(4)`：进程里起一个 worker 池 |
| 多台机器 | `HostConfig(storage_path="postgresql://...")`：多台机器共用一个数据库，用租约（lease）保证同一时刻只有一个 worker 在推进某个任务 |

不需要运维守护进程，中间也没有额外的服务。进程和数据库都是你自己的。
→ [部署上线](guides/deploy.md)

## 另外

- **一切都是插件，内置能力也不例外。** 内核本身不带任何能力。文件工具、网页、记忆、
  MCP、沙箱、存储后端、模型适配器都是插件，和你的插件走同一个加载器；谁想走捷径，
  构建直接失败。插件在一份静态 manifest 里声明自己提供什么，所以不用执行它的任何代码，
  就能列出它的内容、检查有没有冲突。→ [写一个插件](guides/plugins.md)
- **什么模型都能接。** Anthropic、任何兼容 OpenAI `/chat/completions` 的网关、
  OpenAI Responses API。换模型只改一行，agent、它的工具和已有的历史记录都不变。
  → [接入模型](guides/models.md)
- **动手之前先把关。** 权限模式决定哪些工具调用要停下来等审批。guard 可以在调用执行前
  拦下它；observer 只能旁观，所以 observer 出了错也不会拖垮任务。

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

**Claude Agent SDK** 让你的代码在 Claude 上有一个 agent 循环，并替你管好对话。
Noeta 要解决的是另一件事：把 agent 的一次运行变成一份能恢复、能审计、能换机器接着跑的记录。
如果你只想用最省事的方式让 Claude 调工具，用它就好。

**LangGraph** 把 agent 建成一张图，保存的是图状态的检查点。Noeta 没有图，每一步由模型决定；
它保存的是「发生了什么」，而不是「状态当时是什么样」的快照。调度（租约、worker、
回收卡住的任务）都在库里自带。LangGraph 的集成目录和社区要大得多。

**Temporal** 跑的是你事先用代码写好流程的工作流。Noeta 适合流程要由模型边做边摸索出来的工作。
如果步骤是事先确定的，Temporal 更合适。

**Pi 这类终端 agent 工具** 是在终端里交互式地驱动 agent。Noeta 是在你自己的基础设施上无人值守地跑
agent。两者可以配合：终端前端去驱动一个跑在 Noeta worker 池上的任务。

## 什么时候不该用 Noeta

- **你什么都不想运维。** 进程和数据库得你自己管。如果要求是「调厂商 API，零运维」，
  托管的客户端库更简单。
- **你现在就需要大量现成集成。** 内置工具不多，也没有插件市场。
- **一台机器不够，但又没法用 Postgres。** SQLite 和内存存储只支持单机。

更多细节见 [已知限制](operations/limitations.md)。

## 实测成绩

只用公开 SDK 搭的 agent——[noeta-agent](https://github.com/initxy/noeta-agent) 的
`main` 预设，模型 Claude Opus 4.8——在官方评测框架上，Terminal-Bench 2.1 的 40 题抽样首轮解出
**24/40**，每题最多三次取最好 **33/40**（`noeta-sdk` 0.6.28；公开榜单全集区间 58.7%–83.8%）；
SWE-bench Verified 的 15 题子集在重跑 4 个环境准备超时后解出 **13/15**（`noeta-sdk` 0.6.10）。
每项都是在抽样上跑一次，不是全量榜单成绩。→ [基准测试](benchmarks.md)

## 接下来

- [快速上手](start/quickstart.md)：五分钟跑起一个真正的 agent
- [原理总览](how-it-works/index.md)：一页看懂整体设计
