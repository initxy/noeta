# 内置工具

本页是"一个 agent 开箱即可调用的一切"的目录：每个工具做什么、它在风险上让你付出什么代价，以及它究竟要满足什么条件才会出现在模型的工具列表里。

工具名是 provider 安全的 `snake_case`，也是模型调用时使用的确切字符串。每个工具都携带一个 `risk_level`，由它决定一次调用是否需要审批。

一个裸的 `Options()`——也就是 `allowed_tools=None`——会挂载**十一个**工具：`fs` 包（`read`、`glob`、`grep`、`edit`、`write`、`apply_patch`、`shell_run`、`shell_poll`、`shell_kill`）和 `web` 包（`webfetch`、`web_search`）。

```python
from noeta.sdk import Options
options = Options(system_prompt="…")          # allowed_tools defaults to None
# the agent sees: read, glob, grep, edit, write, apply_patch,
#                 shell_run, shell_poll, shell_kill, webfetch, web_search
```

其中十个无需任何配置；`web_search` 需要一个 API key。本页其余的一切都在别处被门控——memory 和 browser 在 agent 激活上，`open_app` 在宿主接线的网关上，`run_skill_script` 在 `skills` 的插件配置上，MCP 在每会话的注册上。

## 文件系统工具

由 `fs` 内置插件的 manifest 声明（`packages/noeta-sdk/noeta/builtins/fs/__init__.py`）。

| 工具 | 风险 | 做什么 | 源码 |
| --- | --- | --- | --- |
| `read` | low | 读一个文件（UTF-8），可按行用 `offset` / `limit` 切片。完整正文总是作为一个 artifact ref 卸载出去。**读取不受围栏限制**——见下文。 | `noeta/builtins/fs/impl/read.py` |
| `glob` | low | 在 `path` 下匹配一个 glob 模式（`**` 递归），返回匹配到的路径，已排序并有上限。 | `noeta/builtins/fs/impl/read.py` |
| `grep` | low | 用 Python `re` 正则做内容搜索，按 `path` 限定范围、按 `glob` 过滤。 | `noeta/builtins/fs/impl/read.py` |
| `edit` | high | 在一个已存在的文件里替换一个精确的 `old` 子串；`replace_all` 把"唯一匹配"切换成"每一处"。 | `noeta/builtins/fs/impl/edit.py` |
| `write` | high | 写一个文件——新建它，或覆盖一个本会话内已经 `read` 过的文件。父目录必须存在；`content` 上限 64 KB。 | `noeta/builtins/fs/impl/edit.py` |
| `apply_patch` | high | 原子地应用至多 16 处 `replace` / `create` 编辑——要么全部成功，要么整批回滚。 | `noeta/builtins/fs/impl/patch.py` |
| `shell_run` | high | 在工作区里运行一条命令；`run_in_background` 让它脱离并返回一个 `job_id`。 | `noeta/builtins/fs/impl/shell.py` |
| `shell_poll` | low | 读取一个后台作业的状态（`running` / `exited`）、退出码和一份新的输出快照。 | `noeta/builtins/fs/impl/shell.py` |
| `shell_kill` | high | 停掉一个你启动的后台作业（SIGTERM，宽限期后 SIGKILL）。 | `noeta/builtins/fs/impl/shell.py` |

当 `HostConfig.write_mode` 为 `"dry_run"`（默认）时，三个写工具只暂存一份提议的 diff，而不碰磁盘；`"apply"` 才执行真实写入。

### 读取不受围栏限制

工作区根围住的是**写入**。对 `read`、`glob` 和 `grep`，它只锚定*相对*路径：一个绝对路径指向哪里就读哪里——隔壁的一份 checkout、某个 skill 包捆绑的参考资料，任何服务器进程能读到的东西。这是刻意的（一个 agent 本来就经常需要读它工作区之外的东西），也正因如此，真正要紧的边界是**进程自身的**文件权限，而不是工作区根。一个不能暴露某条路径的部署，就不该以一个能读到它的用户身份来跑这个 agent。

写入才是受围栏的那一半：`write` / `edit` / `apply_patch` 在工作区根之内解析。`HostConfig.write_roots` 逐次调用地回答"这个任务可以写到它工作区之外的这里吗？"；没有 resolver 时，一次工作区外的写入直接失败。`write` 还额外遵守一个可选的、构造时绑定的工作区相对 `allowed_path_globs` 白名单（为空即不限制）；`edit` 和 `apply_patch` 忽略它。

### Shell 模式

`ShellMode`（`noeta/runtime/shell_policy.py`）在这个包被构建时绑定：

| 模式 | 效果 |
| --- | --- |
| `OFF` | `shell_run` 根本不在这个包里。 |
| `ALLOWLIST` | 默认。只有下面这份结构化白名单能通过，且仅限 argv。 |
| `ARBITRARY` | 任何不含 shell 元字符的命令都经由 bash 运行。 |

在 `ALLOWLIST` 下，以下 argv 模式可以通过（`noeta/builtins/fs/impl/shell_rules.py`）：

- `git status` / `git diff`
- `pytest` / `uv run pytest`
- `npm test` / `pnpm test`
- `grep` / `rg` / `find` / `ls` —— 只读的搜索与列举，因此一个处于 ALLOWLIST 模式、又没有自己的 `grep` / `glob` 工具的 agent 仍然能搜索工作区。它们的校验器会拒绝那些会调起另一个程序或改动文件系统的参数。

宿主配置可以追加更多规则（`{"program": …, "subcommand": …}`）；内置的那些始终保留。运维配置的规则比精心挑选的内置项更宽松：它的意思是"这个程序可以运行"，接受任何通过了元字符扫描的尾部参数。

Shell 元字符（`|`、`;`、`&&`、`>`、…）在分词之前就被拒绝。这是**路径包含加白名单，不是一个进程 sandbox**——`shell_run` 是在受信任的工作区里派生外部程序。

## Web 工具

由 `web` 内置插件的 manifest 声明。

| 工具 | 风险 | 做什么 | 源码 |
| --- | --- | --- | --- |
| `webfetch` | low | 通过 HTTP(S) 抓取一个公开网页并渲染成 Markdown。始终可用。 | `noeta/builtins/web/impl/fetch.py` |
| `web_search` | low | 执行一次网络搜索并把排序后的结果作为 Markdown 返回。**只在设置了 `NOETA_WEB_SEARCH_API_KEY` 时挂载。** | `noeta/builtins/web/impl/search.py` |

## App 工具

| 工具 | 风险 | 做什么 | 源码 |
| --- | --- | --- | --- |
| `open_app` | low | 通过宿主的预览网关发布一个工作区 HTML 应用。只在宿主接上了 `HostConfig.app_gateway` 时挂载。 | `noeta/builtins/app/impl/__init__.py` |

## 记忆工具

只在 agent 激活了 `memory` 时挂载。在官方 preset 中，那就是 `main`（以及内部的整理策展员）。

| 工具 | 风险 | 做什么 | 源码 |
| --- | --- | --- | --- |
| `memory_write` | medium | 向存储写入一个 Markdown 记忆文件。可选的 `description`（一行索引摘要）和 `type`（`user` / `project` / `procedural` / `reference`）会作为一个 frontmatter 块存下来，这个块由工具自己组装。 | `noeta/builtins/memory/impl/store.py` |
| `memory_read` | low | 按需读取一条已存记忆的完整文本。 | `noeta/builtins/memory/impl/store.py` |
| `memory_search` | low | 在名字和全文上做大小写不敏感的子串匹配，返回 grep 风格的摘录（每条记忆至多 3 行，至多 10 条记忆；`truncated` 标志会告诉你还有更多命中）。 | `noeta/builtins/memory/impl/store.py` |
| `memory_archive` | medium | 把一条过时的记忆退役到存储的 `archive/` 子目录——它从索引、召回和搜索中消失，但文件从不被删除，因此人可以把它恢复回来。 | `noeta/builtins/memory/impl/store.py` |

## 浏览器工具

只在**两个条件同时成立**时挂载：agent 激活了 `browser`（`"browser" in AgentSpec.plugins`），并且这个会话绑定到了一个活的 sandbox 容器。在官方 preset 中只有 `web` 子 agent 满足——`main` 自己保持无浏览器并委派给它，因此一个非 sandbox 部署的工具集和稳定前缀不受任何影响。

这五个都是 `high` 风险（任何浏览器动作都可能出站到任意站点），因此除非会话绕过了权限，它们都会走审批。

| 工具 | 风险 | 做什么 | 源码 |
| --- | --- | --- | --- |
| `browser_navigate` | high | 前往一个 `url`；返回页面快照。 | `noeta/builtins/browser/impl/__init__.py` |
| `browser_click` | high | 点击位于 `index` 的可交互元素（来自快照里那份编号列表）。 | `noeta/builtins/browser/impl/__init__.py` |
| `browser_type` | high | 向位于 `index` 的元素输入文本。 | `noeta/builtins/browser/impl/__init__.py` |
| `browser_extract` | high | 把当前页面重新读成一份快照（无参数）。 | `noeta/builtins/browser/impl/__init__.py` |
| `browser_screenshot` | high | 截取一张 PNG 并把它存成一个**工作区 artifact**，返回它的 `ContentRef`。它不会作为视觉输入喂给模型。 | `noeta/builtins/browser/impl/__init__.py` |

四个文本类工具返回一份*页面快照*：页面文本加上编号的可交互元素。`browser_click` / `browser_type` 寻址的正是那套编号，因此必须先有一份快照。

名字、schema 和描述由 noeta 钉死，而不是由容器镜像决定——当 sandbox 改动自己的工具名时，面向模型的契约（因而也包括稳定前缀的缓存字节）绝不能跟着漂移。每个工具都委托给一个 `BrowserBackend`，那是容器浏览器线上协议被钉住的唯一地方。它是一个像 fs 包那样注入的每会话工具包，不是一个 MCP 连接器。

## Skill 工具

| 工具 | 风险 | 做什么 | 源码 |
| --- | --- | --- | --- |
| `run_skill_script` | high | 经由一个白名单解释器运行某个活跃 skill 捆绑的脚本。只在 `skills` 的插件配置设置了 `allow_skill_scripts`、且某个活跃 skill 确实带了脚本时才存在。 | `noeta/builtins/skills/impl/script.py` |

## Control tool

Control tool 是面向模型的 schema，它翻译成 engine 决策，而不是一次 `Tool.invoke`。每一个都是一条会自我门控的 `control_tool` 贡献：挂载*本身*就是启用。

| 工具 | 何时挂载 | 插件 |
| --- | --- | --- |
| `spawn_subagent` | agent 激活了 `delegation`（有子 agent 时自动推导） | `delegation` |
| `todo_write` | agent 激活了 `todo_write` | `todo_write` |
| `ask_user_question` | agent 激活了 `ask_user_question` | `ask_user_question` |
| `skill` | agent 激活了 `skill_invocation` **并且**合并后的 skill 菜单非空 | `skills` |
| `run_workflow` | `HostConfig.workflow_allowed` 打开（且该 agent 能委派） | `react` |
| `structured_output` | 设置了 `Options.output_schema` | `react` |

## MCP 工具

当 MCP 服务器被注册并在某个会话中启用时，远程 MCP 工具会以 `mcp__<alias>__<tool>` 的形式动态出现。见 [ADR: MCP connectors](https://github.com/initxy/noeta/blob/main/docs/adr/mcp-connectors.md)。

进程内的 SDK MCP 服务器（`create_sdk_mcp_server`）不一样：它们的工具保留**裸的** `@tool` 名字，没有 `mcp__` 前缀。见[构建自定义工具](../how-to/build-custom-tools.md)。

## 工具风险等级

恰好有三个等级，顺序是 `low < medium < high`。

| 等级 | 含义 |
| --- | --- |
| `low` | 在 agent 自身状态之外没有副作用。始终允许。 |
| `medium` | 改动持久状态，但只在一个受限目录内——例如记忆存储。 |
| `high` | 修改文件系统、派生外部进程，或触达真实网络。要过审批门。 |

`Options.permission_mode` 决定哪些等级真的会被门控：`"default"` 门控 `low` 以上的一切，`"acceptEdits"` 豁免三个编辑类工具，而 `"bypassPermissions"` 什么都不门控。

## 下一步

- [构建自定义工具](../how-to/build-custom-tools.md) —— 用 `@tool` 加上你自己的
- [Options](sdk-options.md) —— `allowed_tools`、`disallowed_tools`、权限模式
- [Guard 与 Observer](../concepts/guard-observer.md) —— 一次调用如何被拒绝或批准
- [插件 Surface](plugin-surfaces.md) —— 一个工具如何经由一个插件抵达某个 agent
