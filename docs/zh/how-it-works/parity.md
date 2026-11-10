# 如何对齐 Claude Code

在 noeta 里，模型能看到的东西都照着 Claude Code 来：工具名和参数说明、提示词各部分的顺序和角色、提醒、压缩、provider 收到的请求长什么样。底下的机制（事件日志、崩溃恢复、多机运行）是 noeta 自己的。这一页讲为什么界线划在这里、具体对齐了哪些、`make parity` 怎么拿 Claude Code 录下来的请求来核对、`make eval` 怎么检查模型的表现没有变差，以及 noeta 有意不一样的地方。

## 为什么模型能看到的部分是一份约定

模型是对着用得最多的那个编程助手的工具和提示词训练、调优出来的。它们最熟悉的，就是它的工具名（`Read`、`Edit`、`Bash`、`Agent`、`ToolSearch`……）、参数说明、消息格式和各种惯例（`<system-reminder>`、任务完成通知、压缩摘要）。自己另起一套的工具，要用模型的表现来买单，用户也得重新学一套文件和设置。所以（[ADR-003](https://github.com/initxy/noeta/blob/main/docs/adr/003-claude-code-is-the-model-visible-contract.md)）：

- 模型看到的东西，跟最新版 Claude Code 的默认配置一致。有差异就是 bug，除非它连同理由一起写进了 `parity/deviations.md`。
- 底下的实现可以不一样，只要 noeta 的做法更好。
- 能帮助对齐的地方，可以直接用 Claude Code 的原话（[ADR-020](https://github.com/initxy/noeta/blob/main/docs/adr/020-claude-code-wording-is-allowed-where-it-serves-parity.md)）：模型是按 Claude Code 的原话调出来的，只为了"不一样"去改写，是拿模型行为冒险却换不来任何好处。提示词好不好，看它产生的行为（`make eval`、`make parity`），不看措辞从哪来。
- noeta 只读自己的地方（`~/.noeta`、`.noeta/`、`NOETA.md`、`AGENTS.md`），从不读 `~/.claude`、`.claude/` 或 `CLAUDE.md`；但里面的格式都是 Claude Code 的：说明文件的分层、设置、技能、代理文件、hooks、`mcp.json`、插件目录。

没有采用的做法：自己设计一套提示词和工具（在现有模型上明显更差，用户还要学新东西）；什么都原样照搬，却不记录 noeta 必须不同的地方。

## 对齐了哪些

| 方面 | 照 Claude Code 来的部分 |
|---|---|
| 工具 | 名字、参数说明、描述、哪些工具延迟加载、工具清单的顺序、`DeferredToolPlaceholder` |
| 系统提示 | 身份块和主提示词作为两个带缓存的块；主提示词的各节和规则；针对不同模型的段落 |
| 对话的排布 | 项目说明文件和记忆索引作为宿主附加文字放在第一个用户轮；环境、延迟工具清单、日期、预算作为对话中间的系统轮；一轮进行中用户发来的消息，放在工具结果那一轮里并加上说明 |
| 提醒 | 文字内容、各自什么时候发、怎么打标签（`<system-reminder>`）和合并 |
| 请求外层 | 按模型目录给的 `max_tokens`、推理和推理强度参数、beta 请求头那一组、`context_management`、`metadata.user_id`、缓存标记的位置 |
| 超长输出 | 单个结果 50,000 字符的上限、一次回复 200,000 的总额度、`<persisted-output>` 预览 |
| 压缩 | 阈值的具体数值、总结指令和兜底请求、原样保留的尾部、重新读的文件、摘要的说明文字、"反复刚压完又满"和"连续失败"两道保险、提前在后台写摘要、清掉旧结果、闲置时压缩 |
| 对话之外的请求 | 起标题的请求、`WebFetch` 的网页摘要、自动模式的权限分类器 |
| 子代理 | `Agent` 工具、完成通知、交回结果 |
| 文件和设置 | 技能、代理文件、命令、hooks 及其事件、设置项、MCP 配置、Claude Code 插件目录 |

每段提示词背后的规则，都用 noeta 自己的话重写在 `parity/rules/` 下，并附上原文的哈希（主提示词、压缩、提醒、对话之外的请求、子代理、分类器，以及每个工具一个文件）。

## `make parity` 怎么工作

`make parity` 衡量的是：在同一个场景下，noeta 发出的东西和 Claude Code 当时发的有哪些不同。它完全离线：noeta 连到本地一个假的 Messages 接口，这个接口用从 Claude Code 录下的模型回复来作答（`scripts/parity/`）。

<Diagram name="parity-replay" caption="从 Claude Code 录一次；每次运行都对 noeta 重放" />

**录制**（在仓库之外）。`drive.py` 在 tmux 里把每个场景的步骤敲进 Claude Code，遇到提示就按这一步写好的按键作答（`1` 同意，`2` 同意且不再询问，`Escape` 拒绝；没写就选默认高亮的第一项）；`record.py` 是夹在 Claude Code 和上游 Messages 接口之间的代理，记下每个请求和回复。基准是用 API key、默认设置的用户。录下的原始数据放在 `$NOETA_PARITY_RECORDINGS`（默认 `~/cc-recordings`），从不进仓库。场景（`scripts/parity/scenarios.json`）：

| 会话 | 覆盖的内容 |
|---|---|
| `m1` | 自动模式（Claude Code 的默认模式）：主要流程走一遍、中途打断、计划模式、`/compact` |
| `m2` | 默认权限模式：批准、"不再询问"、拒绝，以及子代理的审批 |
| `m3` | 小窗口上的自动压缩（100K × 30%） |
| `m4` | `--continue`：在新进程里恢复 `m1` |
| `m5` | 带项目 `PreToolUse` 钩子的自动模式：一个失败的调用、后台 `Bash` 和 `Monitor`、`TaskStop`、一个让分类器判断的不可逆操作 |

**提炼。** `distill.py` 把录制结果变成 `parity/<version>/` 下的一份约定（现在是 `parity/2.1.312/`）：每个请求的参数名和值、beta 请求头的值和其他请求头的名字、消息的排布、缓存标记的位置、每段文字的类别和它的长度、哈希；`tools.json` 里是每个工具原样的参数说明，但其中所有描述文字（工具的和各个字段的）只留长度和哈希；`texts.json`；以及 `script/`，也就是每个场景里模型那一方的回复。Claude Code 不开源，所以仓库里只放接口层面的事实，不放它的正文。

**重放。** `replay.py` 启动假接口，然后像 `noeta` 命令那样跑 noeta：默认代理、真的 Anthropic provider、全新的存储和 `NOETA_HOME`、场景项目（一个小 git 仓库，带 `NOETA.md`、一个技能、一个 MCP 服务器）放在临时目录里，每个工具都真的执行。主循环的调用按步骤和位置作答，子代理的调用按它的开场提示作答，压缩调用用录下的摘要作答，分类器按录制时的结果作答。审批按这一步录制时的按键作答，和当时操作 Claude Code 的方式一样（`Escape` 拒绝，`2` 同意且以后都放行，其他都是同意）；问题一律选第一项。noeta 发出的每个请求都留在 `parity/out/`。

**比较。** `compare.py` 把 noeta 的每个请求和 Claude Code 同一步的请求逐一对比，写出 `parity/report.md`：每处不同一行，按类别和严重程度排序，各带一个短编号。在 `parity/deviations.md` 里按编号认可过的差异不计入。只要还有差异，运行就以 1 退出。对照 Claude Code 2.1.312，报告是 0 处差异，另有 11 处已认可的差异不计入。

同时还会跑 `own_words.py`：一份仅供参考的报告，列出 noeta 的文字里哪些地方和 Claude Code 录下的文字有很长一段相同，供许可审查时参考。它从不失败（[ADR-020](https://github.com/initxy/noeta/blob/main/docs/adr/020-claude-code-wording-is-allowed-where-it-serves-parity.md)）。

`make parity` 不在 `make check` 里（它要搭场景项目，跑一次要几分钟），但凡是改了给模型看的文字或 provider 的请求格式，都要跑它。`make check` 里有 `tests/agents/test_request_golden.py`，把默认请求的字节锁死。Claude Code 发新版本可能带来新的差异，用新的录制来补齐（步骤见 `scripts/parity/README.md`）。

## `make eval` 怎么工作

和 Claude Code 发的字节一样，并不能证明模型拿到这些字节就会表现良好，尤其是不是 Claude 的模型。`make eval`（`evals/`）用真实模型跑一组防止提示词退化的场景：

- 37 个场景（`evals/scenarios.py`），每个是一个小工作区加一句提问，根据运行轨迹打 0 到 1 分：调用了哪些工具、文件最后是什么样，必要时再让一个模型来评判。例如：`read_over_shell_cat`、`edit_not_write`、`parallel_independent_reads`、`injection_in_file`、`ask_before_ambiguous_delete`、`honest_test_failure`、`compaction_continuity`、`skill_activation`、`memory_recall`、`plan_mode_read_only`、`deny_rule_respected`、`hook_block_adapts`、`mcp_tool_use`。
- 每个场景跑几次（默认 3 次；`make eval-baseline` 跑 9 次并重写 `evals/baseline.json`）。某个场景比基线低出容差（单个场景 0.34，`hook_block_adapts` 放宽到 0.4，总体 0.05，记在基线文件里），运行就失败。
- 凭据从仓库的 `.env` 读（`NOETA_LIVE_BASE_URL`、`NOETA_LIVE_API_KEY`、`NOETA_LIVE_MODEL`）；`--provider` 选 `anthropic`、`openai_compat` 或 `responses`。

parity 说明 noeta 发的和 Claude Code 一样；eval 说明模型拿到这些仍然做对了事。改提示词，两样都要过。

## 有意不一样的地方

`parity/deviations.md` 列出了每一处认可的、模型能看到的差异，一百多行，每行写明理由，以及 pi 和 DeepSeek Harness 在同一件事上的做法。理由大致分几类：

- **内核决定的。** 工具在回复记进日志之后才跑，而不是边流式输出边跑（先写日志才能扛住崩溃）。崩溃前已经开始的工具，答复"结果未知"，除非它自己能查到结果。等待、计时器和 `Workflow` 的运行能扛过重启、能换机器。工具结果按调用顺序返回。
- **持久的任务没有"会话"这回事。** 项目说明文件改了，只要用户发新消息就重新给一次，而不是只在恢复时；两轮之间分支变了，就重新给一次 git 快照；日期变化的提示只会往后走；美元预算从日志里把整棵任务树都算进去。
- **多机和远端执行环境。** 环境信息描述的是执行环境，而不是工人那台机器；压缩摘要只在执行环境是本机时才给出完整记录文件的路径；钩子收到的 `transcript_path` 是空的，因为对话就是日志本身。
- **服务端无人值守。** 连续八次拦住结束的 `Stop` 钩子会被放过；模型目录里没有的模型用保守的上限；`Write` 拒绝超过 8 MB 的内容，因为每次调用的输入都会记进日志。
- **noeta 自己的地方和产品。** Claude Code 写 `CLAUDE.md` 的地方写 `NOETA.md`，写 `.claude/` 的地方写 `.noeta/`；没有计费头那一块（所以系统提示的缓存标记往前挪了一块）；暂时没有 `LSP` 工具、内置技能和输出风格；身份行固定只有一种。

没有写进表里的差异，就是 bug。
