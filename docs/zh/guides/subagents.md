# 子代理与后台任务

这一页讲任务"交出去、过后再拿回来"的那些活：用 `Agent` 派出的子代理、用 `Workflow` 脚本一次派一批、定时触发的提示，以及后台跑的 shell 命令。每一种怎么启动、派活的任务怎么等、怎么被叫醒、子代理继承什么、怎么停、崩溃后还剩什么，都在这里。

一句话：每个子代理都是同一个存储里的一个**子任务**，有自己的日志和收件箱。父任务从不卡在它身上：`Agent` 调用立刻返回，父任务这一轮可以照常结束；子代理的报告和结束通知过后投进父任务的收件箱，把它叫醒。等的这段时间，父任务不占任何工人。

## 相关工具

| 工具 | 所属插件 | 作用 |
|---|---|---|
| `Agent` | `subtasks` | 按列出的类型派一个子代理（或者 `fork` 一份自己）。立刻返回 agent id。 |
| `SubagentHandback` | `subtasks` | 在 auto 模式下给子代理用：把最终报告交给调用方，并结束这次运行。 |
| `SendMessage` | `subtasks`（按需加载） | 给运行中的子代理发消息、让已结束的子代理接着做、写给调用方（`to: "main"`），或写给另一个未结束的任务。 |
| `ListAgents` | `subtasks` | 列出本任务派出的子代理，以及存储里其他未结束的顶层任务。只在主对话里有。 |
| `Workflow` | `workflow` | 跑一段 JavaScript 脚本，里面用 `agent()`、`parallel()`、`pipeline()`、`phase()` 派出多个子代理。立刻返回一个运行 id。 |
| `TaskStop` | `shell`（按需加载） | 停掉后台 shell 命令、`Monitor`、本任务派出的子代理，或一次 `Workflow` 运行。 |
| `Bash` 加 `run_in_background`、`Monitor` | `shell` | 后台命令；结束时（`Monitor` 是每输出一行）回一条通知。 |
| `ScheduleWakeup`、`CronCreate` / `CronList` / `CronDelete`、`/loop` | `schedule` | 过一会儿或按 cron 表达式触发的提示。 |

"按需加载"的工具只把名字告诉模型，要用时再通过 `ToolSearch` 拿到完整定义（见[内置工具](../reference/tools.md)）。插件清单见[内置插件](../reference/plugins.md)。

## 能派哪些子代理

内置的 `main` 代理带着 `subtasks`，参数是 `agents: ["*"]`：工人认识的、写了 `description` 的代理（`main` 自己除外）都是模型可以派的类型。内置的有 `general-purpose`、`Explore`、`Plan`（`noeta/agents/files/`）；你自己放在 `~/.noeta/agents/*.md` 或已信任项目 `.noeta/agents/*.md` 里的代理文件也同样加入（文件格式见[技能、代理与命令](./skills-and-agents.md)）。

这份清单只在第一次请求里发给模型一次，每种类型一行，带上它能用的工具。之后增删了代理文件（或重新加载了插件），模型会收到一条简短说明，列出新增和去掉的类型（`noeta/plugins/subtasks/roster.py`）。模型把类型名写得稍有出入（`general_purpose`、`explore`），只要能唯一对上一个类型，就按那个类型算。

`Explore` 和 `Plan` 带的 `subtasks` 没有给任何类型，所以它们拿不到 `Agent` 工具，不能再往下派；`general-purpose` 可以。

`subagent_type: "fork"` 比较特殊：子任务是调用方的一份副本。它从调用方的整段对话开始（`history_from`、`history_through` 指向父任务的日志），跑的是调用方自己的代理，模型也一定跟调用方一样。fork 出来的任务不能再 fork。

## 派一个子代理

模型调用 `Agent` 时给一个简短的 `description` 和 `prompt`；`subagent_type`、`model`（`sonnet`、`opus`、`haiku`、`fable`）、`effort`（`low` … `max`）和 `isolation`（`worktree`）都可以不给。真正开跑之前，插件会把这一次回复当成一个整体检查一遍（`noeta/plugins/subtasks/tool.py`）：

- 单个调用有问题（提示为空、类型不存在、`effort` 不在那五个值里）就单独拒掉，同一回复里别的调用照常执行；
- 深度到了 `max_depth`（默认 3，主任务是第 0 层）的任务不能再派；
- 一个任务同时在跑的子代理最多 `max_background` 个（默认 20），这一次回复要派的也算在内；
- `isolation: "remote"` 一律拒绝：没有远程隔离这回事。

检查通过后，插件登记子任务（id 形如 `<父任务 id>.<调用 id>.<序号>`），记一条 `launched`，返回"已启动，agent id 是……"。`Agent` 标了 `run_last`，所以同一回复里其他调用先跑完，子代理才开始。

<Diagram name="guide2-task-tree" caption="一棵任务树。每个方框都是存储里的一个任务；接着做的子代理是一个新任务，从旧任务的对话开始。" />

### 子代理继承什么

子任务的配置按下面的顺序拼出来，后面的覆盖前面的（`Subtasks.child_config`、`noeta/core/extension.py` 里的 `child_task`）：

1. 从父任务配置里抄 `inherit_config` 列出的键。默认是 `cwd`、`model`、`effort`；内置 `main` 代理还多传一个 `permission_mode`。
2. 代理定义里自己要求的：它自己的 `model`（除非写的是 `model: inherit`）、`effort`、`permissionMode`、`isolation: worktree`。
3. 这次调用自己给的 `model` 和 `effort`。
4. 父任务每个插件通过 `Extension.inherit` 往下传的东西：权限插件传它记住的规则，以及——如果这个任务中途切换过模式——当前模式；记忆、hooks、项目说明、环境信息和计划模式各传各的。

`model: inherit` 的意思是"用调用方的模型"。内置 `Explore` 有个上限：最高到 Opus 这一档，调用方用 Fable 时它跑在 Opus 上，和 Claude Code 一样（`noeta/agents/agent_files.py` 里的 `BUILTIN_INHERIT_CAP`）。这个上限只管 Claude 官方的模型 id；别的 provider 的模型 id 原样继承。

### 在独立的 worktree 里干活

设了 `isolation: "worktree"`（或代理文件里写 `isolation: worktree`），子代理会拿到一个新的 git worktree，位置是 `<仓库>/.noeta/worktrees/agent-<哈希>`，在一个新分支上，它就在那里干活（`noeta/plugins/subtasks/worktree.py`）。子代理结束时如果什么都没改，worktree 就删掉；如果有改动，就留着，结束通知里写明路径和分支。父任务在子代理还没跑完时就结束了，也按同样的规则收拾它的 worktree。

## 父任务怎么等、怎么被叫醒

父任务不会停在子代理身上等。它接着把这一轮跑完；这一轮结束后，主任务像任何空闲任务一样等输入，把占着的工人还回去。子任务则由随便哪个领到它的工人去跑。

<Diagram name="guide2-child-wait" caption="父任务这一轮结束、不占工人。子代理先交回报告，再是它的结束通知，都投进父任务的收件箱。" />

回来的有两样东西，都走父任务的收件箱：

1. **报告。** 在 auto 模式下（看子代理继承到的权限模式；没装权限插件也当 auto），有这个工具的子代理会拿到 `SubagentHandback`。调用它就把报告当作一条带框的、来源为 `agent` 的消息（`Say`，`when="now"`）投给父任务，把父任务叫醒，同时结束子代理这次运行。报告超过 10 万字符时，全文存进子代理的长输出目录，父任务拿到的是一段预览和文件路径。
2. **结束。** 每个任务结束时都会给父任务投一条 `ChildEnded`（状态、答案、token 用量、工具调用次数）（`noeta/core/runner/closing.py`）。`subtasks` 插件把它变成 Claude Code 的 `<task-notification>`：状态是 `completed`、`failed` 或 `killed`，带一句摘要、一个 `<usage>` 块；子代理留下了 worktree 的话，还有 `<worktree>`。

交回过报告的，结束通知不会再叫醒父任务一次：它先压着，等模型回应完报告，再跟着下一次请求一起发出去。没有交回报告的（不在 auto 模式，或者代理的工具列表里去掉了它），子代理最后一条回复就是通知里的 `<result>`，通知自己叫醒父任务。给了 `SubagentHandback` 却没用就结束的子代理，不带回任何结果；通知会这么说，并建议用 `SendMessage` 去问它要报告。

子代理的任务永远**只跑一轮**：这一轮结束任务就结束，不管它的代理文件怎么写（`TaskRun.one_shot`）。如果它自己也派了后台子代理，结束前会先等它们（`Waiting`，等的是 `children`），因为它们交回的报告可能再开一轮。

子代理运行期间，每 30 秒（前提是它已有三条消息，而且对话有变化）插件会在它的某次模型调用旁边顺带问一句：用三到五个词说说你在干什么。前端把这个标签当提示显示，模型自己看不到。设 `progress_labels = false` 关掉。

每个子代理还有一份对话记录，每条消息一行 JSON，路径是 `<长输出目录>/<根任务 id>/tasks/<agent id>.output`，启动结果和结束通知里都会写出来。它是从日志推出来的：压缩改写了对话、或者别的工人接手了任务时，整份重写。

### 子代理里的审批和提问

子任务需要审批或者要问人时，和任何任务一样先停下：记一条 `Waiting`，不占工人。前端会把整棵任务树都翻一遍（`noeta/host/pending.py` 的 `pending_tree`），所以人在主对话里就能看到并回答子代理的审批。配置里有 `unattended: true` 的任务（宿主给没人盯着的任务设的）遇到这种情况不等，直接以 `needs_human` 结束。

## 给子代理发消息

`SendMessage` 需要 `to` 和 `message`：

| `to` | 结果 |
|---|---|
| 本任务派出的、还在跑的 agent id | 消息进它的收件箱（`when="now"`） |
| 已经结束的 agent id | 子代理**接着做**：新开一个子任务，从旧任务的对话开始（`history_from`），沿用同一个 agent id、同样的配置和 worktree（worktree 已经删了的话，就在调用方的 `cwd` 里干） |
| `main`（子代理发出） | 给调用方的一条说明，标明来自哪个子代理 |
| 另一个顶层任务的 id | 那个任务下一轮会收到 |
| `*` | 拒绝：没有群发 |

在主对话里，`notify_when_idle: true` 可以订阅另一个顶层任务：那个任务空闲下来或结束时，叫醒本任务（最多订 32 个，每个 12 小时后失效；`noeta/plugins/subtasks/idle.py`）。

## Workflow：用脚本派一批子代理

`Workflow` 跑一段按 Claude Code 工作流接口写的 JavaScript。脚本开头是 `export const meta = { name, description, phases }`，后面调用 `agent()`、`parallel()`、`pipeline()`、`phase()`、`log()`，`args` 原样传进去。调用立刻返回一个运行 id（`wf_…`），运行在后台继续。

它为什么扛得住重启（`noeta/plugins/workflow/__init__.py`、`engine.py`）：

- 脚本跑在内嵌的 V8 里（`mini-racer` 包），每次都是全新的上下文：没有模块加载、进程、网络、文件接口，也没有时钟和随机数；每次重放限时 30 秒、堆内存限 256 MB。
- 一次重放只取决于脚本、`args` 和目前记下的结果。已有结果的 `agent()` 直接返回结果，没有的就卡住。卡住时插件把等着的子代理作为子任务启动（同时最多 `max_running` 个，默认是 CPU 数减 2，限在 2 到 16 之间）；每个子任务结束，结果记进日志，再重放一次。
- 日志里只存结果，所以一次运行和普通任务一样能换工人继续。每次运行最多 1000 个子代理。
- 结束时回一条 `<task-notification>`，带脚本的返回值、失败的子代理和用量。每次运行都会把脚本存下来；`scriptPath` 跑改过的脚本，`resumeFromRunId` 让提示和选项没变的 `agent()` 直接复用上次的结果。

Workflow 派出的子代理抄父任务的 `cwd`、`model`、`effort`、`permission_mode`；`agent()` 的选项里可以设 `model`、`effort`、`agentType` 和 `schema`（设了 `schema`，子代理通过 `StructuredOutput` 回答）。存好的工作流放在 `.noeta/workflows/<name>.js` 或 `~/.noeta/workflows/<name>.js`，用 `name` 调用。

模型只在人明确要的时候才用 `Workflow`：提示里出现 `ultracode` 这个词（只管这一轮），或者打开任务配置里的 `ultracode` 开关（一直开着，每 10 条提示提醒一次；`noeta/plugins/workflow/opt_in.py`）。

## 后台 shell 命令

`Bash` 设 `run_in_background: true`，命令就作为后台任务启动，立刻返回 id 和输出文件。前台命令超时（默认 120 秒，最多 600 秒）不会被杀，而是转到后台。`Monitor` 跑一个脚本（或连一个 WebSocket），把 stdout 的每一行当一个事件送回来，直到脚本退出或超时。后台任务结束时，模型收到一条 `<task-notification>`，带退出码和输出文件；任务空闲的话，这条通知会开启新的一轮。

后台命令和子代理不一样：它是**某一台机器上**的一个进程。所以（`noeta/plugins/shell/jobs.py`、[ADR-029](https://github.com/initxy/noeta/blob/main/docs/adr/029-a-task-keeps-its-own-claim-while-it-owns-process-bound-work.md)）：

- 后台命令在跑，它的任务就**一直由这个工人占着**（`ToolContext.keep`，保留认领）。任务那一行写的是 `waiting`，但工人继续续租、继续盯着收件箱，别的工人跑不了这个任务，下一轮也还能停掉这条命令。
- 那个工人挂了，租约到期，另一个工人领到任务，发现手上没有这条命令，就告诉模型它丢了（`JOBS_LOST`）。命令旁边有个看门的小脚本，工人一没动静就杀掉命令，所以卡死的工人留下的命令，不会在别的工人宣布它丢了之后还接着跑。
- 任务结束，它的后台命令跟着死。子代理交回报告时，它的后台命令就结束了；需要命令结果的子代理得先等它跑完再交回。设 `background_ends_with_turn = true`，任务每一轮结束时也会停掉后台命令。
- 一个任务同时最多 8 条后台命令（`max_jobs`）。输出文件只保留前 64 MB。

## 定时提示

`schedule` 插件的唤醒和 cron 任务都是记在任务日志里的内核定时器（`view.set_timer`），所以重启后还在，到点时由当时持有任务的那个工人触发（`noeta/plugins/schedule/__init__.py`）：

- `/loop <提示>` 反复执行一个提示。自己掌握节奏时，模型每轮调用 `ScheduleWakeup`，延迟限在 60 到 3600 秒；它忘了调用的话，20 分钟后有一次兜底唤醒。
- `CronCreate` 接收本地时间的五段式 cron 表达式。周期任务一直触发到被删或满 7 天；`recurring: false` 只触发一次。每个任务会错开一点时间，免得同一分钟的任务一起触发。每个任务最多 50 个。

定时器到点时如果任务空闲，提示就作为下一条消息送进去。

## 怎么停

| 想停的东西 | 做法 |
|---|---|
| 子代理（模型来停） | `TaskStop` 加它的 agent id：它的任务被取消，通知里写"被你停掉" |
| 子代理（人来停） | 在前端取消它的任务，或 `Client.cancel(task_id)`；通知里写"被用户停掉" |
| 一个任务派出的全部东西 | 结束这个任务：结束时会给每个还在跑的子任务投 `Cancel` |
| 只停父任务当前这一轮 | `Client.cancel(task_id, turn_only=True)`（终端界面里按 Esc）：没跑完的调用被关掉，某个调用正等着的子任务被取消；后台子代理继续跑 |
| 一次 `Workflow` 运行 | `TaskStop` 加运行 id：运行以 `killed` 结束，在跑的子代理被取消 |
| 一条后台命令 | `TaskStop` 加命令 id；父任务也能这样停掉子任务的命令（投进子任务的收件箱） |
| 循环或 cron 任务 | `ScheduleWakeup` 带 `stop: true`；`CronDelete` |

## 设置

上面这些都是插件参数，写在 `~/.noeta/config.toml` 里，对所有代理生效（代理文件自己给的参数优先）：

```toml
[plugins.subtasks]
max_depth = 2              # 到这一层就不能再派（根是 0）；默认 3
max_background = 8         # 一个任务同时在跑的子代理；默认 20
progress_labels = false    # 不要 30 秒一次的进度标签
queue = "gpu"              # 子任务建在这个队列上（默认跟调用方一样）

[plugins.workflow]
max_running = 4            # 一次运行同时跑的子代理数

[plugins.shell]
max_jobs = 4                       # 每个任务的后台命令数；默认 8
background_ends_with_turn = true   # 每轮结束时停掉后台命令
```

建在别的队列上的子任务，只有服务那个队列的工人才会跑（`noeta worker --queue gpu`），见[作为服务端运行](./server.md)。

## 出问题时

- **子代理跑着跑着工人挂了。** 没什么特别：子代理就是个任务，租约到期后别的工人从日志接着跑，和父任务无关。
- **`Agent` 调用跑到一半崩了。** 启动记录和调用结果是一起写的，所以没写完的调用什么都没启动。恢复时模型看到的结果是"不确定"，它建的 worktree 如果没有改动就删掉。
- **交回报告跑到一半崩了。** 恢复时重做一遍；父任务按去重键丢掉重复的那份。
- **子任务结束了、通知还没生成就崩了。** 父任务下次启动时，把已经收到但没变成通知的结束补成通知（`Subtasks.on_start`）。
- **子代理失败。** 通知状态是 `failed`，带上错误。跑到步数上限的子代理算 `completed`，但会注明结果不完整。
- **后台命令所在的工人挂了。** 模型会被告知这条命令丢了，不会自动重跑。

相关页面：[等待与唤醒](../how-it-works/waiting.md)、[单写者与崩溃恢复](../how-it-works/recovery.md)、[多机运行](../how-it-works/many-machines.md)。
