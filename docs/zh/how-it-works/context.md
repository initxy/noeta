# 上下文、缓存与压缩

这一页讲三件事：每次调用模型时 noeta 发了什么、按什么顺序发；为什么任务重启或换一台机器接着跑，发出去的字节还是一模一样；以及一个很长的任务怎么一直待在模型的上下文窗口里——超长输出存成文件、清掉旧的工具结果、把对话压缩成摘要。

这一切都建立在[任务日志](./event-log.md)那一页的一条规则上：下一次模型请求，只由任务折叠出来的状态决定。凡是两次运行之间可能不一样的东西（磁盘上的文件、日期、插件的设置、一段摘要），第一次用到时就记进日志，之后都从日志里读。

## 一次请求由哪些部分组成

`build_request`（`noeta/core/loop/request.py`）把折叠出的 `TaskState` 变成一个 `ModelRequest`；`build_model_request` 和 `call_model`（`noeta/core/runner/model.py`）给它配上代理、各插件给的部分和工具清单，记下这一步的提醒，跑完所有 `before_model`，然后发出去。

<Diagram name="ctx-request-layout" caption="一次请求从上到下的样子（Anthropic 接口的发法）" />

| 部分 | 放了什么 | 从哪来 |
|---|---|---|
| 工具 | 任务提供的所有工具，按名字排序；标了 `listed_last` 的（一组会变的工具，比如服务端工具）排在后面，同样按名字排。延迟加载的工具带上 `defer_loading`；被扣下的不发。不是工具的条目（`ToolEntry`，比如 `DeferredToolPlaceholder`）放在它指定的位置。 | `offer_tools`（`noeta/core/extension.py`）：各插件的 `tools(view)`，套上代理的允许和禁止清单，再加上各插件的 `tool_changes`。`request_tools` 负责排序。工具的说明是一段有名字的提示词 `tool/<Tool>`。 |
| 系统提示，第 1 块 | 身份那一行。 | 提示词 `identity/<agent>`：代理文件里写的 `identity`，没写就是 `You are noeta, an open-source coding agent.`（`noeta/agents/agent_files.py`）。 |
| 系统提示，第 2 块 | 代理的提示词，后面按 `order` 接上各插件的 `ContextPart`。带 `before` 的一段插在提示词里和它相同的那一行前面（比如放在某个标题之前）。 | 提示词 `system/<agent>`（代理文件的正文）和 `Extension.context(view)`；记忆那一节和任务清单那一行就是两个内置的例子。 |
| 消息 | 折叠结果里的对话：任务开始时记下的背景、用户的消息、模型的回复、工具结果、各种提示，以及这一步的提醒。 | `TaskState.messages`（`noeta/core/loop/state.py`）。 |

开头那些背景信息不在系统提示里。`instructions` 插件在 `on_start` 里读项目说明文件（`NOETA.md`、`AGENTS.md`、`.noeta/rules`、记忆索引），作为第一句提问前的一条消息记下来；`environment` 插件用同样的办法记下 git 快照和提交署名的说明，而环境信息（工作目录、平台、模型、日期）则作为第一次请求里的一个系统轮。放在对话里，同一个代理的所有任务系统提示就完全一样，压缩之后也能把它们放回摘要旁边（自动压缩放在摘要前面）。

有两个插件会在 `before_model` 里改系统提示的文字：`environment` 只留下写给当前模型的那几段，对不需要告知预算的模型去掉 `<total_tokens>` 那一行（`noeta/plugins/environment/notes.py`），并且除非任务配置里写明它的前端能跑 `!` 命令（`bash_mode`，终端界面会设），否则去掉讲 `! <命令>` 的那一行；`skills` 在任务没有技能、或者没有 `Skill` 工具时，把提示词里讲技能的那一行删掉。这两行都在 `# Session-specific guidance` 标题下，两行都删掉后标题也一起去掉（`noeta/toolkit/guidance_lines.py` 里的 `drop_guidance_line`）。另外，用户要求 `ultrathink` 的那一轮，`environment` 会按 `xhigh` 的推理强度发。

### 提示词的名字和覆盖

每一段给模型看的文字都有名字（`noeta/core/prompts.py`）：`system/<agent>`、`identity/<agent>`、`tool/<Tool>`、`<plugin>/<key>`（比如 `compaction/summarize`、`memory/section`）、`reminder/<key>`。默认文字写在拥有它的代码里。覆盖文件替换它，`<name>.append` 则在它后面空一行再追加。覆盖的来源，优先级从低到高：

1. 宿主程序的提示词目录（`noeta` 命令用 `~/.noeta/prompts`；文件 `tool/Read.md` 覆盖 `tool/Read`，`tool/Read.append.md` 追加到它后面）；
2. 任务的工作区 `<cwd>/.noeta/prompts`，只有工作区受信任时才读；
3. 任务配置里的 `prompts` 表（`config.toml` 的 `prompts`，或 SDK 宿主给的配置）。

这些文件只在任务第一次启动时读一遍（最多 256 个文件，每个不超过 256 KB），作为内核的 `prompts` 记录写进日志。之后不管在哪台工人上恢复，都用记下来的文字，所以改提示词文件只影响改动之后新开的任务。

### 提醒（reminder）

提醒是给模型看、但不是用户说的话：环境信息、延迟加载的工具清单、日期、预算、某个文件变了、想起来的一条记忆。插件在 `reminders(view)` 里返回 `Reminder`；每次调用模型之前，运行器挨个问一遍所有插件，把每条提醒作为一条带消息的 `ExtensionRecorded` 记下来，再把这些记下的消息放进这一次请求、接在对话后面（`noeta/core/runner/model.py` 里的 `_remind`）：

- 先放 `role="user"` 的：作为宿主附加的文字并进用户那一轮（`origin` 是 `system`，想起来的记忆是 `memory`）；
- 再放 `role="system"` 的，按 `order` 排序，每条是后面的一个系统轮。`environment` 插件就靠 `order` 安排自己的几条（环境 0、延迟工具 30、语言 75、预算 80、日期 90）。

因为提醒是记下来的，从此它就是对话的一部分：下一次请求会在同一个位置再带上它，任务换到别的工人上恢复，发出去的字节也一样。只需要在任务开始时取一次的文字，在 `on_start` 里记；要把等待中的任务叫醒的输入，一律走[收件箱](./waiting.md)，不用提醒。

各家 provider 的接口代码会把宿主附加的文字处理成不可能被当成用户说的话。Anthropic 收到的是用户那一轮里的 `<system-reminder>` 块，以及对话中间的 `system` 轮（相邻的合并成一个，和 Claude Code 发环境、预算提示的方式一样）。OpenAI 的两种接口收到的是一条 system 消息，开头一行说明这是宿主程序自动附加的背景、不是用户的话（`noeta/backends/providers/_common.py` 里的 `HOST_INJECTED_PREAMBLE`）。用户或工具写出来的文字没法伪造这个标签：里面的保留标签会被中和掉。

### 延迟加载的工具

`ToolFlags` 里设了 `deferred=True` 的工具，只把名字告诉模型，等模型用 `ToolSearch` 加载之后才发完整定义（Claude Code 的延迟加载，见 [ADR-017](https://github.com/initxy/noeta/blob/main/docs/adr/017-deferred-tool-loading-is-a-plugin.md)）。MCP 工具默认都延迟加载，这样接了二十个服务器，也不会让每次请求都背着它们的全部参数说明。

- `tool_search` 插件（`noeta/plugins/tool_search`）通过 `tool_changes` 加上 `ToolSearch`，并扣下所有还没加载的延迟工具。模型直接调用一个被扣下的工具，会在任何权限检查之前就收到一个错误，告诉它先加载。
- 某个工具结果里点名了它（一个 `ToolReference` 块），这个工具就算加载了。插件把这些名字和那个结果一起记下，所以压缩不会把工具"卸载"，从别的任务接着跑的任务也保留来源任务已加载的工具。
- 请求里总带着 `DeferredToolPlaceholder`，放在倒数第二个。它不会被调用，作用是让模型厂商的延迟加载模式从第一次请求起就打开，第一个工具加载时工具清单的形状也不变。
- `environment` 插件在第一次请求的系统轮里列出所有延迟工具，之后集合有变化（某个 MCP 服务器连上或断开）再告诉模型。

加载放在插件里而不是内核里，是因为 `tool_changes` 给每个插件看的是同一份"已提供的工具"：谁的结论都不取决于别的插件排在前还是后；内核在拼请求之前统一扣掉这些工具，请求依旧只由日志决定。

## 提示词缓存

provider 的提示词缓存是这样计费的：请求开头的一段（到某个缓存标记为止）如果和最近的某次请求逐字节相同，这一段就按很低的价格算。noeta 的请求就是为了每一步都能命中它而拼的。

<Diagram name="ctx-cache-breakpoints" caption="第 N+1 步从缓存读出第 N 步的前缀，只写入新增的部分" />

**标记打在哪。** `build_request` 给两个系统块都打标记（`SystemPart(cache=True)`），再给最新的一条消息打一个（`cache_breakpoints`：只打一个，打在最后一条消息上，和 Claude Code 一样）。Anthropic 的接口代码（`noeta/backends/providers/_anthropic_wire.py` 里的 `apply_cache_control`）把它们写成请求里的 `cache_control`，从新到旧，最多四个（API 的上限）。工具不单独打：Anthropic 的前缀顺序是工具、系统提示、消息，第一个系统块上的标记把前面的工具一起缓存了。所以一次主请求带三个标记。

**为什么打在最新一条消息上。** 下一步的请求就是这一次的请求，加上模型的回复和工具结果：这一步最后一个标记之前的内容都从缓存读，只有新的回复和结果要写入。一步里新加的提醒在请求发出之前就记下了，标记在它们之后重新放一次，所以标记打在的那条消息，下一次请求里也有。

别的请求自己放标记：总结调用把标记打在总结指令前面那条消息上，这样它能直接读主请求的缓存；自动模式下的权限分类器给自己的几块打标记。

**缓存能活多久。** 默认要的是 API 的 5 分钟缓存。Anthropic 的 provider 设了 `prompt_cache_ttl = "1h"`（写在 `config.toml` 里默认 provider 的设置上，或某个 `[providers.<name>]` 表里），就要一小时的缓存（`{"type": "ephemeral", "ttl": "1h"}`）。写入更贵，但任务经常停下超过 5 分钟的话划算。回复里会说明它写的缓存能活多久（`ModelResponse.cache_ttl`），折叠进 `TaskState.cache_ttl`，压缩插件靠它判断一个闲着的任务的缓存什么时候过期。

OpenAI 的两种接口没有标记可打；那边的服务器自己缓存前缀，同样稳定的前缀对它们一样有用。

### 字节为什么能保持不变 {#why-the-bytes-stay-the-same}

缓存要命中，前缀得逐字节相同，重启之后、换一台工人之后也一样。每个可能变的地方都被钉住了：

| 可能变的东西 | 怎么让它不变 |
|---|---|
| 工具的顺序 | 按名字排；会变的那组标 `listed_last`，放在前缀最后。 |
| 提示词覆盖文件 | 任务开始时读一次，记下（`prompts`）。 |
| 项目说明文件、git 状态、日期、环境信息 | 在 `on_start` 或第一次请求时取，作为消息或插件记录记下。说明文件改了，就作为一条新消息再给一次，不去改旧的那条。 |
| 用了哪些插件、带什么参数 | 每个任务都钉住（`records.Plugins`）；缺插件的工人不跑这个任务（见[插件版本记录](./plugins.md#the-plugin-pin)）。 |
| 模型走哪个 provider、上限多少 | 选定模型时记进任务配置（`model_route`，见[模型路由](./models.md#routing-with-several-providers)）。 |
| 系统提示里插件给的部分 | `ContextPart` 必须是稳定的文字；会变的内容改成记一条消息。 |
| 什么时候压缩、什么时候清理 | 按记下的用量和一个固定算法的估算来定，不用分词器（`noeta/plugins/compaction/estimate.py`：一个中日韩字符算一个 token，其他文字四个字符算一个），两台机器算出来一样。 |
| 摘要、被清掉的结果、重新读的文件 | 都记下（`Compacted` 和压缩插件自己的记录），从不重算。 |

每个 `ModelCalled` 事件都记着 `request_hash`，即整个请求的哈希，同一步的两次运行可以直接从日志对比。有几种变化是故意打破缓存的：切换模型、压缩、清理旧结果，都会开始一个新的前缀。

## 超长的工具输出 {#long-tool-outputs}

工具返回的文字太长时，整份存成一个模型能 `Read` 的文件，模型只看到一段预览（Claude Code 的规则，`noeta/core/large_output.py`）。

<Diagram name="ctx-large-output" caption="超出上限的结果变成任务执行环境里的一个文件，加一段 2 KB 的预览" />

- **每个工具的上限。** 结果长过这个工具的 `ToolFlags.max_result_chars`（默认值也是最大值 50,000 个字符，`RESULT_CEILING_CHARS`）就存文件。`Bash` 自己先截到 30,000；`Read` 设成 `None`，自己控制输出长度。
- **一次回复的总量。** 一次回复里所有调用的文字结果加起来有 200,000 个字符的额度（`REPLY_RESULTS_BUDGET_CHARS`）。用完之后，之后每个超过 2,000 字符的结果也存文件。同时跑的几个调用在开始等待之前就先占好自己那份，不会一起冲过额度。
- **模型看到什么。** 一个 `<persisted-output>` 块：大小、路径，以及前 2,000 个字符（在换行处截断）。
- **文件放在哪。** 经由任务的执行环境写到输出存放目录（spill root）下：`<spill root>/<task id>/<call id>.result.txt`（`result_spill_path`）。路径只由任务和调用决定，同一个调用重跑还是这个文件名。因为是经由执行环境写的，下一步不管在哪台工人上跑，模型都能用自己的文件工具读到。`noeta` 命令的存放目录是 `~/.noeta/tool-results`；宿主程序会定期清理，已结束任务的目录在最后一次写入 30 天后删除（`noeta/host/spill_sweep.py`）。
- **存不了的时候。** 没有存放目录（`Runtime(spill_root=None)`）时，超长的结果只留开头和结尾，中间换成一句说明（`<truncated-output>`）。写文件失败时，结果原样保留，和 Claude Code 一样。
- 出错的结果、带图片的结果、挂起的调用都原样通过。

子代理最后交回来的报告也按同样的方式限制在 100,000 个字符（`noeta/plugins/subtasks/tool.py` 里的 `RESULT_LIMIT_CHARS`）。

## 怎么一直待在窗口里 {#staying-inside-the-window}

任务一长，总会碰到模型的窗口上限。noeta 有和 Claude Code 一样的一整套办法，从便宜到贵排列，分别在内核和 `compaction` 插件（`noeta/plugins/compaction/`）里：

| 办法 | 什么时候 | 默认 |
|---|---|---|
| 调小输出上限再发一次 | provider 说输入放得下，只是加上 `max_tokens` 放不下 | 一直开 |
| 清掉旧的工具结果 | 停了一段时间、提示词缓存已经凉了 | 关 |
| 提前在后台写摘要 | 上下文超过有效窗口的大约 80% | 开 |
| 压缩 | 上下文到了阈值 | 开 |
| 超长被拒之后压缩 | provider 以太长为由拒了请求 | 开 |
| 闲着的时候压缩 | 一小时缓存、上下文很大、在缓存过期前一直闲着 | 关 |
| `/compact [重点]` | 用户要求 | 一直可用 |

### 怎么量、几条线在哪

每次调用模型之前（一轮中途也算），压缩插件都量一次上下文：最新一条回复报告的用量（输入加输出），加上那之后新增内容的估算。还没有任何回复报告用量时，以及刚压缩完的时候，就估算整个请求。压缩之后的第一次请求一律原样发出。

<Diagram name="ctx-window" caption="模型窗口被切成的几段（noeta/plugins/compaction/window.py）" />

这些数字和 Claude Code 一样从模型目录算出来。写摘要需要地方，所以有效窗口要预留 `min(输出上限, 20,000)` 个 token。阈值比有效窗口低 13,000；如果设了 `auto_compact_percent` 并且算出来更低，就用有效窗口的这个百分比。从有效窗口的 80% 起开始在后台写摘要。`auto_compact_window` 给这些数字的起点（窗口大小）设一个上限。插件没给这两个参数时，分别读环境变量 `NOETA_AUTO_COMPACT_WINDOW` 和 `NOETA_AUTOCOMPACT_PCT_OVERRIDE`（Claude Code 的两个开关，换成 noeta 的前缀）。模型目录里没有的模型，按保守的 128,000 窗口、16,384 输出上限算，照样会压缩。

阈值一变，插件就记一条 `threshold` 记录，前端的"距自动压缩还剩 N%"读的就是它。

### 一次压缩做了什么

<Diagram name="ctx-compaction" caption="自动压缩前后的对话" />

1. **先问别的插件。** 每个插件的 `before_compact(view, trigger, instructions)` 可以给摘要加几句要求，也可以返回 `Deny` 跳过这次压缩（会告诉用户原因）。hooks 插件在这里跑 Claude Code 的 `PreCompact` 钩子。
2. **决定总结哪些。** 最新一条回复之前的全部内容；这条回复和它之后的内容原样保留。如果最新这条回复回答的是上次压缩后的第一个请求，就全部总结。最新回复之前没有值得总结的东西时，自动压缩先不做。
3. **写摘要。** 总结调用就是主请求，在最后一条用户消息后面加上总结指令（`compaction/summarize`），这样能直接读主请求的缓存；设了 `compaction_model` 就发给另一个模型。回复里的 `<analysis>` 丢掉，留下 `<summary>`。调用失败或者没给出摘要，就再发一次更朴素的兜底请求（换成一个只负责总结的系统提示，只带 `Read` 工具——开了工具搜索时再加上 `ToolSearch` 和 MCP 工具，图片换成占位文字，不打缓存标记）。总结请求本身太长时，最早的几轮被去掉，最多三次，摘要里会说明这一点。每次调用的花费都作为内核的 `spent` 记录记下。
4. **给摘要加上说明。** 摘要消息说明它代替了前面的一段对话，列出之前用过的工具，给出完整记录文件的路径（执行环境是本机时：`~/.noeta/projects/<workspace>/<task id>.jsonl`，从日志重新折叠出来），让模型不必复述、直接接着干，并说明后面是否还跟着原样保留的新消息。
5. **把该留的放回去。** 各插件交回必须留下的内容（`lead_resident`：重新读一遍的项目说明、记忆索引、现取的 git 快照和提交署名说明；`resident`：用过的技能、MCP 服务器的说明、计划、还在跑的后台命令和子代理）。环境信息、延迟工具清单和子代理清单随下一次请求重新发一遍。对话里最近碰过的文件，最多五个，通过 `Read` 重新读一遍（每个 5,000 token，总共 50,000），放进下一次请求的系统轮。
6. **记下来。** 以上全部写进一条内核的 `Compacted` 事件：`summary`、它覆盖到的 seq（`through`），以及排在摘要前面的（`lead`）、紧跟摘要的（`kept`）、排在原样保留部分之后的（`after`）消息；外加插件自己的 `compacted` 记录。折叠时把 `through` 之前的历史换成这些（`noeta/core/loop/state.py` 里的 `_compact`），`before_model` 抛出 `RequestStale`，于是所有插件都按压缩后的状态重新拼一遍请求。

手动的 `/compact [要求]` 做的是同样的事，只是加上用户指定的重点，摘要前面什么都不放，而且这一轮不再调用模型就结束（`TurnDone`）。

**出了问题怎么办。** 自动压缩的摘要失败时，这次请求按原样发出（如果真的超长，之后会走"超长被拒之后压缩"）；连续失败三次，这个任务就关掉自动压缩，`/compact` 和超长之后的压缩照常可用。如果在同一轮里，上下文在上次压缩后三次模型调用之内又满了，并且连续三回这样，这一轮就以 `autocompact_thrashing` 停下：说明有某个文件或输出单独就塞不进窗口，会提示用户分段读。压缩做到一半进程崩了，重启后会从日志拼出同样的总结请求再做一遍，因为那时它还什么都没记下。

### 提前在后台写摘要 {#writing-the-summary-ahead}

从有效窗口的大约 80% 起，较早几轮的摘要在后台写（`auto_compact_precompute`，默认开），到了阈值直接换上，那一步不用再等总结调用。难点在于：一个任务只有一个写者，每次请求都只由日志决定，所以在这一步之外算出来的结果，不能由算它的那段代码直接写进去（[ADR-031](https://github.com/initxy/noeta/blob/main/docs/adr/031-background-plugin-work-returns-through-the-inbox.md)）：

<Diagram name="ctx-precompute" caption="后台写好的摘要经收件箱回来" />

- 越过 80% 的那一步记下 `precompute_started` 和一个随机的任务编号（job id），用这一步的请求发起调用。这个调用只存在于插件的内存里，不决定任何字节。
- 调用结束后，结果和花费作为一个 `External` 信号（`compaction.precomputed`）投进任务自己的收件箱。运行器在下一个边界像取别的输入一样取出它——不管那时任务在哪台工人上；插件的 `on_event` 只有在编号和日志里记着"正在写"的那个一致时，才把摘要记下。别的（过时的，或者别人投的）只记下花费。
- 记下的摘要写明了它覆盖的范围：压缩次数和那段消息的指纹（digest）。到阈值（或超长被拒、或闲置压缩）时，只有两者都还对得上才用它；否则这一步照常现场总结。到阈值时还没写完的摘要不会去等。
- 工人死了，正在跑的调用就丢了；之后的某一步会再起一个。失败三次后，这个任务不再提前写。

### 清掉旧的工具结果

停顿的时间一旦超过提示词缓存的寿命，下一次请求反正命不中缓存，这时把请求变小几乎没有代价。打开 `auto_compact_clear_results` 后，距上一次模型调用超过 `auto_compact_clear_idle_seconds`（默认 3,900 秒），那些工具的 `clearable` 属性为真的较早调用，结果会换成 `[Old tool result content cleared]`（`noeta/plugins/compaction/clearing.py`）。最新的 12 个保留，已经存成文件的保留；省不下至少 20,000 个 token 就什么都不清。被清的调用编号会记下来，之后的每次请求都按这条记录改写。写摘要时仍然读到这些结果的原文。

### 闲着的时候压缩

用一小时缓存并打开 `auto_compact_idle` 时，一轮结束后上下文还有至少 200,000 个 token，就设一个内核计时器，在缓存寿命的 90% 处触发。触发时任务若还闲着（晚了不超过 60 秒，也没有更新的请求），就用 `TaskView.next_request` 在缓存过期前压缩，并告诉用户。子代理不做这种压缩。

### provider 说请求太长的时候

每个 provider 都把自己的错误归成几类中立的错误（`noeta/backends/providers/_common.py`）：413，或者错误码是 `context_length_exceeded`、消息里说提示太长的 4xx（说限流或输出上限的不算），归为 `ContextOverflow`。如果消息里写明了输入长度和窗口大小（Anthropic 的 `input length and max_tokens exceed context limit: 188240 + 21333 > 200000`，OpenAI 和 vLLM 的 `maximum context length is …` 几种写法），重试逻辑（`noeta/core/retry.py`）就把 `max_tokens` 调成 `窗口 − 输入 − 1,000`，只要不低于 3,000、并且比这次调用原本要的上限小，就再发一次：输入本身放得下，只是加上要求的输出放不下。否则就挨个问各插件的 `on_context_overflow(view, request)`；压缩插件做一次压缩（提前写好的摘要还对得上就用它，否则把全部内容总结掉），然后重新调用。谁都腾不出地方，这一轮以 `context_overflow` 失败。

## 日志留着全部，折叠结果只留下一步要用的

<Diagram name="ctx-log-fold" caption="ADR-030：日志是唯一完整的记录" />

压缩改变的是折叠结果，从不改日志。事件日志是唯一完整的记录；`TaskState` 只留模型看得到的对话（被压缩控制住长度）、几项内核需要的事实（`prompt_seq`，即用户最新一条消息的 seq；用量；计时器），以及各插件自己的状态（[ADR-030](https://github.com/initxy/noeta/blob/main/docs/adr/030-the-log-is-the-only-full-transcript.md)）。插件如果需要压缩之前的某件事，就在它发生时记进自己的状态（`tool_search` 加载过的工具、memory 读过的页），很少用到的就去读日志（完整记录文件就是这样生成的）。从别的任务接着跑的任务，带走的是来源任务压缩后的对话和各插件状态，更早的不带。这样[快照](./event-log.md)的大小跟着下一步需要的东西走：一个压缩过 14 次的测试任务，快照从 255 KB 降到 5.9 KB。

## 预算 {#budgets}

有三种不同的限制在看任务花了多少：

- **整个生命周期的上限**（`budget` 插件，`noeta/plugins/budget.py`）：`max_iterations`（也可以写 `max_turns`，指模型调用次数）、`max_tool_calls`、`max_cost_usd`、`max_spawned_subtasks` 和 `max_subtask_depth`（默认 3）。超过上限的工具调用由 `before_tool` 拒掉并说明原因。计数是插件自己的记录，从日志折叠出来，所以每台工人执行的是同样的上限；只有设了的上限才计数。
- **美元预算提示**（`noeta/toolkit/usd_budget.py`）：任务配置里设了 `max_budget_usd`（`noeta -p --max-budget-usd`）时，每次调用模型都会告诉它整段对话（根任务和它下面的所有任务）到目前为止花了多少、还剩多少，数字从各任务的日志里读。无界面协议在超出后以 `error_max_budget_usd` 结束。
- **token 预算提示**：Claude Code 会告知预算的那些 Claude 模型，会在一个系统轮里收到 `<total_tokens>N tokens left</total_tokens>`，N 是 15,000,000 减去这一轮已经用掉的输出 token（`noeta/plugins/environment/notes.py`）。

## 代码在哪

| 部分 | 文件 |
|---|---|
| 拼请求、缓存标记、请求哈希 | `noeta/core/loop/request.py` |
| 调用模型的一步：提醒、`before_model`、超长处理 | `noeta/core/runner/model.py` |
| 提示词的名字和覆盖 | `noeta/core/prompts.py` |
| 工具清单、`ContextPart`、`Reminder`、`ToolChanges` | `noeta/core/extension.py` |
| 超长输出 | `noeta/core/large_output.py` |
| Anthropic 请求格式、缓存标记、缓存寿命 | `noeta/backends/providers/_anthropic_wire.py`、`anthropic.py` |
| 超长错误的识别 | `noeta/backends/providers/_common.py` |
| 重试、调小输出上限 | `noeta/core/retry.py` |
| 压缩、清理、后台写摘要、闲置压缩 | `noeta/plugins/compaction/` |
| 延迟加载的工具 | `noeta/plugins/tool_search/` |
| 环境、日期、预算提示 | `noeta/plugins/environment/` |
| 项目说明文件 | `noeta/plugins/instructions/` |
| 生命周期上限 | `noeta/plugins/budget.py` |

各插件的参数见[内置插件](../reference/plugins.md)，配置项见[配置项](../reference/config.md)。
