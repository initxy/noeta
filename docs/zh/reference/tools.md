# 内置工具

agent 开箱就能调用的全部工具：每个做什么、风险等级多高、什么条件下才会挂上。

裸的 `Options()`（`allowed_tools=None`）会挂上 `fs` 和 `web` 两组工具：

```python
from noeta.sdk import Options
options = Options(system_prompt="…")          # allowed_tools defaults to None
# the agent sees: Read, Glob, Grep, Edit, Write,
#                 Bash, BashOutput, KillShell, WebFetch, WebSearch
```

`WebSearch` 还要设置 `NOETA_WEB_SEARCH_API_KEY`。其余工具各有开关：

| 工具 | 什么时候挂上 |
| --- | --- |
| `fs`、`web` 两组 | 总是挂上（受 `allowed_tools` / `disallowed_tools` 过滤） |
| `memory_*` | agent 启用了 `memory` |
| `browser_*` | agent 启用了 `browser`，**并且**任务绑定了一个在跑的沙箱 |
| `open_app` | 宿主设置了 `HostConfig.app_gateway` |
| `run_skill_script` | `plugin_config["skills"]["allow_skill_scripts"]` 打开，且某个已启用的技能带了脚本 |
| `mcp__<alias>__<tool>` | 这个任务注册并启用了远程 MCP server |
| 控制类工具 | 见[控制类工具](#控制类工具) |

## 文件系统工具

来自内置插件 `fs`（`noeta/builtins/fs/`）。

| 工具 | 风险 | 参数 | 做什么 |
| --- | --- | --- | --- |
| `Read` | low | `file_path`、`offset?`、`limit?` | 读 UTF-8 文件，可按行截一段。完整内容另存为 artifact 引用。 |
| `Glob` | low | `pattern`、`path?` | 列出匹配 glob 的路径（`**` 递归），排好序并有上限。用 `rg --files` 遍历：遵守 gitignore，跳过隐藏文件。 |
| `Grep` | low | `pattern`、`path?`、`glob?`、`type?`、`output_mode?`、`-i`、`-n`、`-o`、`-u`、`-A`/`-B`/`-C`、`context?`、`head_limit?`、`offset?`、`multiline?` | 用 ripgrep 搜内容，经 `ExecEnv` 执行（环境里必须装有 `rg`）。 |
| `Edit` | high | `file_path`、`old_string`、`new_string`、`replace_all?` | 精确替换一段文本（默认要求唯一匹配，`replace_all` 则全部替换）。文件必须先 `Read` 过。 |
| `Write` | high | `file_path`、`content` | 新建文件（自动建父目录），或覆盖本任务里已经 `Read` 过的文件。`content` 上限 8 MB。 |
| `Bash` | high | `command`、`timeout?`（毫秒，最多 600000）、`description?`、`run_in_background?` | 在工作区根目录执行命令。后台模式返回一个作业 id。 |
| `BashOutput` | low | `bash_id`、`filter?` | 查后台作业的状态（`running` / `exited`）、退出码和新输出。 |
| `KillShell` | high | `shell_id` | 停掉后台作业（先 SIGTERM，宽限期后 SIGKILL）。 |

- **默认不真写盘。** `HostConfig.write_mode="dry_run"`（默认）只记录一份拟改的 diff；`"apply"` 才真正写入。
- **写有边界，读没有。** `Write` / `Edit` 只能写工作区根目录以内；`HostConfig.write_roots` 可以按任务放开更多目录。`Read` / `Glob` / `Grep` 只把*相对*路径锚到工作区，绝对路径指到哪就读哪，所以真正的读取边界是进程自己的文件权限。
- `Write` 可以在构造时绑定一个相对工作区的 `allowed_path_globs` 白名单（空 = 不限）；`Edit` 不看它。

### Shell 审批

`SdkHost.shell_mode`（默认 `ShellMode.ALLOWLIST`）和权限模式一起决定 `Bash` 能跑什么：

| 设置 | 行为 |
| --- | --- |
| `shell_mode=OFF` | 不挂 `Bash`。 |
| `default` / `acceptEdits` | 命令经 `bash -c` 执行。命中白名单的直接跑，其余要审批。 |
| `bypassPermissions` | 什么命令都跑，不审批。 |

内置白名单（`noeta/builtins/fs/impl/shell_rules.py`）只认不含 shell 元字符的命令：

| 程序 | 接受 |
| --- | --- |
| `git status` | 无参数、`--short`、`-s`、`--porcelain` |
| `git diff`、`git log` | 只读用法 |
| `pytest`、`uv run pytest` | 跑测试 |
| `npm test`、`pnpm test` | 任意后续参数 |
| `grep`、`rg`、`find`、`ls` | 只读；拒绝 `rg --pre`/`--hostname-bin` 和 `find -exec`/`-delete`/`-fprint*` |

扩展白名单有三种办法：

| 来源 | 格式 | 说明 |
| --- | --- | --- |
| `SdkHost.shell_allowlist` | `[{"program": …, "subcommand": …}]` | 运维配置的规则；后续参数只要过得了元字符检查都放行 |
| `<workspace>/.noeta/shell-allowlist.json` | 同样的 JSON 列表 | 属于仓库内容，只有工作区被信任（`grant_trust`）时才加载；否则每个工作区警告一次 `UntrustedProjectShellAllowlistWarning` |
| `SdkHost(project_shell_allowlist_trust="open")` | — | 无条件加载工作区文件；`trust_store=` 可指向别的信任记录 |

::: warning
这只是白名单加审批，不是进程沙箱。`Bash` 起的是真进程，服务端用户能写的地方它都能写。要隔离请用[沙箱](../guides/sandbox.md)。
:::

## 网页工具

| 工具 | 风险 | 参数 | 做什么 |
| --- | --- | --- | --- |
| `WebFetch` | low | `url`、`prompt` | 抓网页、转成 Markdown，再用一次辅助模型调用按 `prompt` 回答（`Options.webfetch_model`，默认用任务的主模型）。HTTP 自动升 HTTPS；跨域名重定向不跟随，交回给模型；页面缓存 15 分钟。只支持 `http(s)`。 |
| `WebSearch` | low | `query`、`count?` | 网页搜索，返回排好序的 Markdown 结果。只有设置了 `NOETA_WEB_SEARCH_API_KEY` 才挂上。 |

`WebFetch` 什么地址都能访问。`HostConfig.webfetch_allowed_hosts` 列出不用问人就能访问的域名：

| 写法 | 匹配 |
| --- | --- |
| `example.com` | 只匹配这个域名 |
| `*.example.com` | 任意层级的子域名，**不含** `example.com` 本身 |

| 权限模式 | 名单外的域名 | 名单内的域名 |
| --- | --- | --- |
| `default`、`acceptEdits` | 每次调用都要审批 | 直接访问 |
| `bypassPermissions` | 直接访问 | 直接访问 |

写法不合法会在构造 `HostConfig` 时报错。匹配看的是 URL 真实的域名（转小写、IDNA 规范化），`https://example.com@evil.test/` 算作 `evil.test`。交回给模型的重定向，下一次调用会重新判断。这只是遇到陌生域名时问一下人，不是出网边界——有 `Bash` 的 agent 一条 `curl` 就能出去。真要管出网，请在网络层或沙箱里做。

## 应用工具

| 工具 | 风险 | 参数 | 做什么 |
| --- | --- | --- | --- |
| `open_app` | low | `dir`、`proxy_to` | 通过 `HostConfig.app_gateway` 发布工作区里的 HTML 应用。 |

## 记忆工具

agent 启用 `memory` 时挂上（预设里是 `main` 和后台整理记忆的 agent）。

| 工具 | 风险 | 参数 | 做什么 |
| --- | --- | --- | --- |
| `memory_write` | medium | `name`、`text`、`description?`、`type?`、`keywords?`、`related?` | 写一条 Markdown 记忆。frontmatter 按字段合并进磁盘上已有的（不传 = 保留，传空 = 删除）。自动写入 `created` / `updated` / `source_task`；新名字会提示相似的已有记忆。 |
| `memory_read` | low | `name` | 读一条记忆的全文。 |
| `memory_search` | low | `query` | 在名字和正文里做不分大小写的子串搜索；每条最多 3 行摘录，最多 10 条，超出时带 `truncated` 标记。 |
| `memory_archive` | medium | `name` | 把记忆移到 `archive/`：不再出现在索引、召回和搜索里，但文件不删。 |

`type` 取 `user` / `project` / `procedural` / `reference`。`keywords` 是逗号分隔的检索别名（跨语言召回靠它）；`related` 列出召回这条时一并带上的其他记忆名。

## 浏览器工具

只有 agent 启用了 `browser` 且任务有在跑的沙箱时才挂上。预设里只有 `web` 子 agent 满足。全部是 `high` 风险。

| 工具 | 参数 | 做什么 |
| --- | --- | --- |
| `browser_navigate` | `url` | 打开网址，返回页面快照。 |
| `browser_click` | `index` | 点击上一份快照里编号为 `index` 的元素。 |
| `browser_type` | `index`、`text` | 往编号元素里输入文字。 |
| `browser_extract` | — | 重新读取当前页面快照。 |
| `browser_screenshot` | — | 截一张 PNG 存进 `ContentStore`，返回 `ContentRef`。不会作为图片喂给模型。 |

快照 = 页面文字 + 带编号的可交互元素。工具名和参数由 Noeta 固定，不随容器镜像变化。

## 技能工具

| 工具 | 风险 | 参数 | 做什么 |
| --- | --- | --- | --- |
| `run_skill_script` | high | `skill`、`relpath`、`args?` | 用白名单里的解释器运行已启用技能自带的脚本。不经过 shell。 |

## 控制类工具

这些工具交给模型的是一份 schema，调用后变成引擎的决策，而不是执行 `Tool.invoke`。启用名写进 `Options.plugins`；写错会在构建时抛 `ValueError`，并列出合法名字。

| 工具 | 什么时候挂上 | 启用名 / 插件 |
| --- | --- | --- |
| `Task` | agent 能派子任务（有 `agents` 时自动推出） | `delegation` |
| `TodoWrite` | agent 启用了它 | `todo_write` |
| `AskUserQuestion` | agent 启用了它 | `ask_user_question` |
| `skill` | 启用了它，且合并后的技能菜单非空 | `skill_invocation`（由 `skills` 挂上） |
| `run_workflow` | `HostConfig.workflow_allowed=True` 且 agent 能派子任务 | `react` |
| `RecallHistory` | 接了上下文压缩——在 `Client` / `query` 下总是接的 | `react` |
| `structured_output` | 带独立 schema 派出的子任务 / workflow 助手（`Options.output_schema` 走 provider 原生的结构化输出） | `react` |

`RecallHistory` 按 `offset` 翻回被压缩进摘要的原始消息——这些内容不在任何文件里。

## MCP 工具

远程 MCP 工具名是 `mcp__<alias>__<tool>`。进程内的 SDK server（`create_sdk_mcp_server`）保留 `@tool` 的原名。见 [MCP server](../guides/mcp.md)。

## 风险等级

| 等级 | 含义 | `default` 下要审批 |
| --- | --- | --- |
| `low` | 不影响 agent 自身状态以外的东西 | 否 |
| `medium` | 在限定目录里做持久写入（比如记忆库） | 是 |
| `high` | 写文件、起进程、访问外网 | 是 |

`Options.permission_mode`：`"default"` 对 `low` 以上的工具都要审批；`"acceptEdits"` 再额外放行 `Edit` / `Write`；`"bypassPermissions"` 全部放行。`Bash` 和 `WebFetch` 另有上面说的逐次检查。

## 下一步

- [自定义工具](../guides/tools.md)——用 `@tool` 加自己的工具
- [Options](options.md)——`allowed_tools`、`disallowed_tools`、`permission_mode`
- [引擎](../how-it-works/engine.md)——一次调用是怎么被批准或拒绝的
