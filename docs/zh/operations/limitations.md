# 已知限制

按目前的代码，noeta 不做什么、哪些地方做得不如你预期。每条都注明来历：是定下来的决定（没有新决定不会变），
还是记在
[`docs/specs/2026-10-29-open-source-follow-ups.md`](https://github.com/initxy/noeta/blob/main/docs/specs/2026-10-29-open-source-follow-ups.md)
里的待办（可能会变）。和 Claude Code 在模型可见层面的每一处差异，连同理由，都列在
[`parity/deviations.md`](https://github.com/initxy/noeta/blob/main/parity/deviations.md)；本页只挑你最可能碰到的。

## 版本与平台

- **还是开发版。** 包版本是 `1.0.0.dev0`，1.0 重做的规格仍是 `active` 状态
  （[`docs/specs/2026-10-26-noeta-1-0-redesign.md`](https://github.com/initxy/noeta/blob/main/docs/specs/2026-10-26-noeta-1-0-redesign.md)）。
- **只支持 Linux 和 macOS。** 包声明的平台就这两个，Windows 上没有构建也没有测试。
- **需要 Python 3.11 或更新。**
- **macOS 的沙箱比 Linux 的松。** Seatbelt 配置仍允许 `mach-lookup` 和 Apple Events（待办：需要一台 Mac 来测）。
  Linux 的系统调用过滤只认 x86_64 和 aarch64。

## 一个存储只服务一方

一个存储加一个服务端只服务一方。拿到服务端令牌的人能操作所有任务，工具以工人进程的系统用户身份运行。
没有租户、没有用户、没有按人配额
（[ADR-028](https://github.com/initxy/noeta/blob/main/docs/adr/028-no-tenants-in-noeta.md)）。
要把几方隔开，就给每一方单独的服务端和存储 schema，或者在宿主代码里自己做隔离（见[作为服务端运行](../guides/server.md)）。

## 多台机器

任务的大部分内容都在日志里，可以在工人之间自由迁移。下面这些不行：

- **工作目录。** 命令和文件工具都作用在任务的 `cwd` 上，从每台工人看过去必须一样：要么是挂在同一路径的共享文件系统，
  要么是远程执行环境。noeta 不会在机器之间拷文件（见[命令在哪里执行](../guides/sandbox.md)）。
- **后台 `Bash` 命令**只活在一个工人进程里。命令在跑时，任务一直由那台工人认领着；那台工人死了，模型会被告知命令丢了。
  没法重新接上。
- **粘贴的图片**每台工人各写一份，接手任务的工人会重新写出来。接着另一个任务的对话继续的任务（`history_from`）
  不会复制原任务的粘贴图片：原任务的图片目录被清理后，这些路径就失效了（待办）。
- **实时输出**（模型逐字吐出的内容）通过 Postgres 通知跨机器，尽力而为。前端的 `LISTEN` 建好之前发出的部分不会补发，
  但记进日志的完整回复照样会到（待办）。用 sqlite 时，实时输出只留在一个进程里。
- **升级要整体停机。** 不同版本的工人不能共用一个存储：全停、升级、再启动。比存储 schema 旧的进程会拒绝打开它。
- **每台工人的插件必须一致。** 缺了任务开始时所用插件的工人永远不会跑这个任务；同一台工人连续三次认领到它，
  任务以 `plugin_missing` 失败（见[排错](./troubleshooting.md)）。
- **快照装着整段对话。** 2 万个事件的日志，快照大约 12 MiB，所以在 Postgres 上认领一个长任务主要卡在传输上（待办）。
- **死掉的工人留下的后台命令日志目录**（`w<pid>`），如果在非本机的执行环境里，不会被清理（待办）。

## 命令与沙箱

- **沙箱是可选的，默认关闭。** 不开沙箱时，命令以工人的系统用户身份运行，权限规则是唯一的一道关卡。
  没设 `failIfUnavailable` 的话，沙箱起不来（比如没装 `bwrap`）命令就不受限地跑，只在启动时给一条通知。
- **沙箱计划解析完路径、到 `bwrap` 挂载之间，如果有链接被换掉，察觉不到**（待办）。
- **访问 `sandbox.network.allowedDomains` 之外的网络一律拒绝**，不会在命令跑到一半时来问你；Claude Code 会问。
- **`credentials` 里 `mask` 模式的条目按 `deny` 处理**（文件藏起来，变量清掉）。
- **`EnterWorktree` 建的工作树在沙箱里是只读的**（待办）。
- **没有 `/sandbox` 命令。** 在设置的 `sandbox` 部分里配置。
- **`Bash` 不加载任何 shell 启动文件**（不读 `~/.bashrc`，也不读登录配置），用的是 `PATH` 上的 `bash`。
  Claude Code 会先抓一份你登录 shell 的环境。
- **`Bash` 只保留命令输出的最后 256 KB**，超过 30,000 个字符的部分存到文件里；没有 `BASH_MAX_OUTPUT_LENGTH` 可调。
- **`Write` 拒绝超过 8 MB 的内容**：每次调用的输入都会记进日志，并在之后的每个请求里重发。

## 你可能会注意到的、和 Claude Code 的不同

- **配置位置。** noeta 从不读 `~/.claude`、`.claude/`、`CLAUDE.md`、`.mcp.json` 和 `CLAUDE_*` 变量。
  格式是 Claude Code 的，位置是 noeta 自己的（`~/.noeta`、`.noeta/`、`NOETA.md`、`AGENTS.md`）。
- **只有终端界面开的任务才有那条 `! <命令>` 规则**（模型会建议你用 `!` 自己跑交互式命令），和 Claude Code 只在交互式终端里给一样。用了 `--server` 时没有，因为那时终端界面不执行 `!` 命令；`-p`、ACP、HTTP、网页端也都没有。
- **没有 `LSP` 工具**；Claude Code 插件里的语言服务器、监视进程（monitors）、`bin/`、主题、输出风格都不加载
  （见[和其他 agent 对比](./comparison.md)）。
- **没有代理团队（agent teams）**，`Agent` 也没有 `name` 参数。
- **`WebSearch` 要 Tavily 的 key**（`NOETA_WEB_SEARCH_API_KEY`），结果里每条是网页摘录，不是搜索模型写的小结。
- **不自带技能。**
- **`context: fork` 的技能总是在后台跑它的代理**，`-p` 下也一样。
- **技能：** 技能 frontmatter 里的 `hooks:` 不执行；`skillOverrides` 不读。
- **hooks：** hook 输入里的 `transcript_path` 是空的（完整记录就是任务日志，要通过客户端读）。`Stop` / `SubagentStop`
  hook 连续 8 次阻止这一轮结束后就放行。`TaskCreated`、`TaskCompleted` 以及 noeta 没有对应功能的其他 Claude Code 事件
  （`Setup`、`ConfigChange`、`FileChanged` 等）会被跳过并打警告。
- **MCP：** MCP prompt 或 @ 引用的 resource 文本截到 64 KiB。工具的 schema 按服务器给的原样发给 provider
  （Claude Code 会去掉 API 会拒绝的那些）。名字超过 128 个字符的工具不提供。ACP 走 `--server` 时传不了编辑器的
  MCP 服务器（那里面有编辑器的凭据）；Claude Code 插件的 MCP 服务器会被 ACP 编辑器的服务器顶掉（待办）。
- **`AskUserQuestion`** 的回答不带每题备注，没有自由作答，也没有超时。既没有交互式前端、也没有权限提问工具时，不提供这个工具。
- **模型目录里没有的模型**按 128,000 token 窗口、16,384 token 输出上限处理（Claude Code 假定 200,000 和 32,000）。
  网关上的模型请在模型文件里写上真实上限。
- **`WebFetch` 按域名询问**，Claude Code 预先放行的那些文档站点也一样问（待 owner 定）。
- **任务清单：** 子代理没有自己的清单；全部完成的清单会一直留着，直到把里面的条目删掉。

## 已知的毛边

记在待办里、细心的运维可能会碰到的问题：

- **关停时启动子进程。** MCP stdio 服务器（`noeta/plugins/mcp/stdio.py`、`pool.py`）和 hooks 执行器
  （`noeta/plugins/hooks/runners.py`）用 asyncio 子进程启动进程；CPython 3.14 之前，如果事件循环在启动途中被关掉，可能永远等下去。
- **聊天渠道。** 任务结束之后才重投的聊天消息，会在接续的新任务里再处理一遍（一次性代理会回答两次）。接续任务的 id
  用 `.N` 后缀，碰上本身就以 `.<数字>` 结尾的聊天线程 id 时可能撞车。
- **后台命令日志。** 空日志不会因字节上限被清掉；等待结束后还活着的写日志进程，可能在清理后把日志又建出来
  （`noeta/plugins/shell/jobs.py`）。
- **记忆召回。** 挑选记忆的那次调用，在拒绝 `thinking: disabled` 或忽略输出 schema 的网关上会失败（会重试，然后打警告）；
  召回和 Claude Code 一样默认关闭。记忆插件排在 hooks 前面时，挑选调用仍会看到被 hook 拦下的那条输入（主模型看不到）。
- **工作树里的信任。** 隔离运行的子代理所在的工作树不算受信任的项目，在那里回答的「总是允许」不会存进项目设置。
- **runner 的记账。** `Runtime._running` 是集合而不是计数；两件小概率的事同时发生时，一个任务可能以同一个 epoch 被再次接管。
- **粘贴图片的保存。** 写到一半崩溃留下的残缺文件不会重写。

## 决定不做的

有意不做，并附理由；要改需要新的决定。

- **不发遥测、不上传日志、不检查更新**
  （[ADR-010](https://github.com/initxy/noeta/blob/main/docs/adr/010-nothing-uploaded-by-default.md)）。
  只会联系你配置的模型 provider。
- **不给插件代码套沙箱。** 插件就是工人进程里的 Python，CPython 关不住它。隔离放在真能做到的地方：执行环境、MCP 服务器、命令式 hook。
- **插件文件里不能定义 provider**：任务记下的插件清单不涵盖 provider，只装在一台工人上的 provider 会破坏「任何工人都能接着跑任何任务」。
- **没有托管的插件市场，也不自动更新。** 安装固定到某个提交，安装过程不执行插件里的任何东西。
- **插件不能改写发给 provider 的原始请求**：请求必须能从日志重新算出来。
- **崩溃后不重跑 MCP 工具调用**：不信任服务器自报的 `idempotentHint`。
- **短 PDF 仍以页面图片发送**，不用文档块，这样 `/model` 切到别的 provider 时还能继续。
- **模型 provider 不支持订阅登录**：服务端用的是运维方的 key。
