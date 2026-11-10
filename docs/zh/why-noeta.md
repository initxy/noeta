# 为什么是 noeta

这一页讲清楚 noeta 是做什么用的、适合谁、刻意不做什么，以及它和 Claude Code 是什么关系。

## 它是什么

noeta 是一个开源的编程 agent：站在模型那边看，它的行为和 Claude Code 一样；站在运维那边看，
它是按服务端程序的路子造的。它是一个 Python 包（`noeta`），上面有终端界面（`noeta-tui`），
另有网页版（`noeta web`）和桌面版，用的都是同一套接口。它和别的 agent 不一样的地方
在于这两点同时成立：对模型守着 Claude Code 的约定；你又可以自己部署任意多个 worker，大家共用一个
Postgres 存储，哪个 worker 都能接着跑任何任务。

两个决定定下了其余的一切：

1. **模型看到的东西照 Claude Code 来。** 模型最熟悉的是最常用的那个编程 agent 的工具和提示词。
   noeta 发同样的工具名和参数格式，保留 Claude Code 提示词各部分的规则和顺序（但用自己的话写），
   发同样的提醒和通知，用同样的方式压缩上下文，发给 provider 的请求结构也一样。任何差异都算 bug，
   除非它连同理由一起登记在案；`make parity` 会重放录下来的 Claude Code 真实会话来核对
   （[如何对齐 Claude Code](./how-it-works/parity.md)、[ADR-003](https://github.com/initxy/noeta/blob/main/docs/adr/003-claude-code-is-the-model-visible-contract.md)、
   [`parity/deviations.md`](https://github.com/initxy/noeta/blob/main/parity/deviations.md)）。
2. **底下，一个任务就是一条只追加、只有一个写入者的日志。** 正在跑的任务没有任何东西只活在某个
   进程里。worker 认领任务，重放日志得到状态，走一步，再把发生的事追加进去。任何 worker 都能接手
   任何任务，崩溃后不会稀里糊涂地重复有副作用的操作，等人回复的任务什么资源都不占
   （[ADR-001](https://github.com/initxy/noeta/blob/main/docs/adr/001-task-is-an-event-log-with-one-writer.md)、
   [任务就是一条日志](./how-it-works/event-log.md)）。

## 适合用来做什么

- **多台机器。** 让任意多个 `noeta worker` 进程连同一个 Postgres 存储，前面放 `noeta serve`；
  前端连哪台服务端都行（[作为服务端运行](./guides/server.md)、[用 Docker 部署](./guides/docker.md)、
  [多机运行](./how-it-works/many-machines.md)）。
- **无人值守的编程 agent。** 一个任务可以跑好几个小时；它所在的 worker 被杀掉或重新部署了，
  别的 worker 会从日志断的地方接着跑。
- **需要等人的 agent。** 审批和提问会让任务挂起，不占任何进程。你可以在终端、编辑器、HTTP 接口
  或自己的服务里回复，几分钟后或几天后都行。
- **嵌进你的程序。** 同一个内核也是 Python 库：在你的程序里创建任务、跟踪事件、回答它的提问
  （[在 Python 里嵌入](./start/embedding.md)）。
- **任何模型。** Anthropic 的 API、任何兼容 OpenAI 的接口（vLLM、各种网关、DeepSeek）、OpenAI 的
  Responses API，可以同时配好几个，每个模型自动发到提供它的那家（[模型与 provider](./guides/models.md)）。
- **给 Claude Code 写的程序。** `noeta -p` 支持 Claude Code 无界面模式的参数和 stream-json 协议，
  基于 Claude Code Agent SDK 写的程序不用改就能驱动 noeta（[终端界面与 -p](./guides/terminal.md)）。

## 适合谁

- 想要 Claude Code 那种干活方式、又想自己选模型的开发者，在终端、编辑器或浏览器里用。
- 在服务器上跑编程 agent 的团队：CI 任务、代码审查机器人、长时间的重构、在聊天工具里回话的 agent。
- 在 agent 之上做产品、需要状态持久、可审计（日志就是完整记录）、能分布到多台机器的人。
- 写插件的人：所有能力都是同一个接口下的插件，一个插件可以就是一个 Python 文件
  （[写一个插件](./guides/plugins.md)）。

## 它不是什么

- **不是 Claude Code 的代码。** noeta 是独立实现。提示词是自己写的；只有规则、顺序和模型能看到的
  格式照 Claude Code 来。
- **不读 Claude Code 的文件。** noeta 只读 `~/.noeta/`、项目里的 `.noeta/`、`NOETA.md` /
  `AGENTS.md`，以及机器的管理员目录。里面的格式是 Claude Code 的，所以迁移就是复制一下；但
  `~/.claude/`、`.claude/`、`CLAUDE.md`、`.mcp.json` 从来不读，两个工具不会互相跑对方的 hooks
  或服务器
  （[ADR-004](https://github.com/initxy/noeta/blob/main/docs/adr/004-noeta-reads-only-its-own-config-places.md)）。
- **不做多租户。** 一个存储、一台服务端只服务一方：谁拿着服务端的 token，谁就能操作所有任务，
  工具以 worker 的系统用户身份运行。要隔开不同的人，就给他们各自的服务端和存储，或者在扩展点上
  自己做隔离（[ADR-028](https://github.com/initxy/noeta/blob/main/docs/adr/028-no-tenants-in-noeta.md)）。
- **不是工作流引擎。** 不需要画流程图。每一步由模型决定；固定的流程就是一个代理加几个子代理
  （或者像 Claude Code 那样用 `Workflow` 工具）。
- **不是托管服务。** 它跑在你的进程里、你的机器上、你的存储上。没有 noeta 云，也不往回报信。
- **不是 Claude Code 有的它都有。** Claude Code 插件里的 monitor、`bin/` 目录、LSP 服务器、主题和
  输出风格不会加载；有些 hook 事件属于 noeta 没有的 Claude Code 功能，会跳过并给出警告。完整清单见
  [和其他 agent 对比](./operations/comparison.md)和[已知限制](./operations/limitations.md)。
- **还很新。** 1.0 是这套代码的第一个正式版本。0.x 版本是另外的 `noeta-runtime` / `noeta-sdk`
  包，说的是另一套代码。

## 和 Claude Code 的关系

| | Claude Code | noeta |
|---|---|---|
| 模型看到的东西 | 标准 | 照着它来；差异登记在 `parity/deviations.md`，由 `make parity` 检查 |
| 设置、hooks、技能、代理、MCP、插件目录 | `~/.claude`、`.claude/`、`CLAUDE.md`、`.mcp.json` | 同样的格式，放在 `~/.noeta`、`.noeta/`、`NOETA.md` / `AGENTS.md`、`.noeta/mcp.json` |
| 环境变量开关 | `CLAUDE_*`、`MAX_MCP_OUTPUT_TOKENS` 等 | 同样的开关，换成 `NOETA_` 前缀 |
| 无界面使用 | `claude -p`、stream-json、Agent SDK | `noeta -p`，参数和协议相同 |
| 一次对话 | 本机上的一次会话 | 一个任务：存储里的一条日志，任何 worker 都能接着跑 |
| 工具执行到一半崩溃 | 会话结束 | 下一个 worker 接着跑；没跑完的调用答成"结果未知"，除非工具自己能查到结果 |
| 等待审批 | 有个进程在等 | 任务挂起，不占 worker |
| 模型 | Anthropic 的 | Anthropic、兼容 OpenAI 的接口、OpenAI Responses，可以同时用好几个 |
| 怎么扩展 | 插件、hooks、MCP | 这些都支持，另外还有统一接口 `Extension` 下的 Python 插件 |
| 遥测 | 有，可以关 | 没有 |

对齐是怎么保证的（录下 Claude Code 的真实请求、离线回放比对、登记允许的差异），见
[如何对齐 Claude Code](./how-it-works/parity.md)。和 pi、DeepSeek Harness 等其他 agent 的逐项对比见
[和其他 agent 对比](./operations/comparison.md)。
