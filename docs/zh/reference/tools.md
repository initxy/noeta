# 内置工具

工具名称是 provider 安全的 `snake_case`，是模型调用的确切字符串。每个工具携带一个 `risk_level`，供 `PermissionGuard` 读取。

`Options.allowed_tools=None` 选中这 11 个名字的**内置白名单**——`fs` 包（`read`、`glob`、`grep`、`edit`、`write`、`apply_patch`、`shell_run`、`shell_poll`、`shell_kill`）加上 `web` 包（`webfetch`、`web_search`）。其中十个无需额外配置即可挂载；`web_search` 需要一个 API key。本页其余的一切都在别处被门控：memory 和 browser 在 agent 激活上，`open_app` 在主机接线的网关上，`run_skill_script` 在 `allow_skill_scripts` 上，MCP 在每会话注册上。

## 文件系统工具

由 `fs` 内置插件清单声明（`packages/noeta-sdk/noeta/builtins/fs/__init__.py`）。

| 工具 | 风险 | 用途 | 来源 |
| --- | --- | --- | --- |
| `read` | low | 读取文件（UTF-8），可选按行 `offset` / `limit` 切片。完整正文总是作为 artifact ref 卸载。**读取不设围栏**——见下文。 | `noeta/builtins/fs/impl/read.py` |
| `glob` | low | 在 `path` 下匹配一个 glob 模式（`**` 递归），返回匹配路径，已排序并封顶。 | `noeta/builtins/fs/impl/read.py` |
| `grep` | low | 用 Python `re` 正则做内容搜索，以 `path` 限定范围并按 `glob` 过滤。 | `noeta/builtins/fs/impl/read.py` |
| `edit` | high | 替换现有文件中精确的 `old` 子串；`replace_all` 从唯一匹配切换到全部出现。 | `noeta/builtins/fs/impl/edit.py` |
| `write` | high | 写入文件——创建它，或覆盖本会话已 `read` 过的文件。父目录必须存在；`content` 封顶 64 KB。 | `noeta/builtins/fs/impl/edit.py` |
| `apply_patch` | high | 原子性地应用至多 16 个 `replace` / `create` 编辑——全部成功，否则整批回滚。 | `noeta/builtins/fs/impl/patch.py` |
| `shell_run` | high | 在工作区中运行命令；`run_in_background` 将其分离并返回一个 `job_id`。 | `noeta/builtins/fs/impl/shell.py` |
| `shell_poll` | low | 读取后台作业的状态（`running` / `exited`）、退出码和一份最新输出快照。 | `noeta/builtins/fs/impl/shell.py` |
| `shell_kill` | high | 停止你启动的后台作业（SIGTERM，宽限期后再 SIGKILL）。 | `noeta/builtins/fs/impl/shell.py` |

在 `HostConfig.write_mode` 为 `"dry_run"`（默认）时，三个写工具暂存一个提议的 diff，而不触碰磁盘；`"apply"` 执行真实写入。

### 读取不设围栏

工作区根为**写入**设围栏。对 `read`、`glob` 和 `grep`，它只锚定*相对*路径：绝对路径会在其所指之处被读取——一个相邻的 checkout、一个 skill 包捆绑的参考资料，任何 server 进程能读的东西。这是刻意为之的（agent 时常需要读取其工作区之外的内容），也正因如此，真正要紧的边界是**进程自身的**文件权限，而不是工作区根。一个绝不能暴露某路径的部署，就不应以能读取它的用户来运行 agent。

写入是设围栏的那一半：`write` / `edit` / `apply_patch` 在工作区根内部解析。`HostConfig.write_roots` 按调用回答“这个任务可以写在这里、写在它的工作区之外吗？”；没有 resolver 时，一次工作区外写入直接失败。`write` 另外遵循一个可选的、工作区相对的 `allowed_path_globs` 白名单，在构造时绑定（空 = 无限制）；`edit` 和 `apply_patch` 忽略它。

### Shell 模式

`ShellMode`（`noeta/runtime/shell_policy.py`）在构建包时绑定：

| 模式 | 效果 |
| --- | --- |
| `OFF` | `shell_run` 根本不在包里。 |
| `ALLOWLIST` | 默认。只有下面的结构化允许列表通过，仅限 argv。 |
| `ARBITRARY` | 任何不含 shell 元字符的命令都会经 bash 运行。 |

在 `ALLOWLIST` 下，这些 argv 模式可以通过（`noeta/builtins/fs/impl/shell_rules.py`）：

- `git status` / `git diff`
- `pytest` / `uv run pytest`
- `npm test` / `pnpm test`
- `grep` / `rg` / `find` / `ls` —— 只读的搜索与列举，因此一个处于 ALLOWLIST 模式、没有自带 `grep` / `glob` 工具的 agent 仍能搜索工作区。它们的校验器拒绝那些会调起另一个程序或改动文件系统的标志。

主机配置可以追加更多规则（`{"program": …, "subcommand": …}`）；内置规则始终保留。一条操作者配置的规则比精心策划的内置规则更宽松：它的意思是“这个程序可以运行”，接受任何通过了元字符扫描的尾部参数。

Shell 元字符（`|`、`;`、`&&`、`>` 等）在分词之前被拒绝。这是**路径包含加上一个允许列表，而非进程沙箱**——`shell_run` 在受信任的工作区中生成外部程序。

## Web 工具

由 `web` 内置插件清单声明。

| 工具 | 风险 | 用途 | 来源 |
| --- | --- | --- | --- |
| `webfetch` | low | 通过 HTTP(S) 获取公共网页并渲染为 Markdown。始终可用。 | `noeta/builtins/web/impl/fetch.py` |
| `web_search` | low | 运行一次 Web 搜索并以 Markdown 返回排名命中。**仅在设置 `NOETA_WEB_SEARCH_API_KEY` 时挂载。** | `noeta/builtins/web/impl/search.py` |

## App 工具

| 工具 | 风险 | 用途 | 来源 |
| --- | --- | --- | --- |
| `open_app` | low | 通过主机的预览网关发布工作区 HTML 应用。仅在主机接线了 `HostConfig.app_gateway` 时挂载。 | `noeta/builtins/app/impl/__init__.py` |

## 记忆工具

仅在 agent 激活 `memory` 时挂载。在官方预设中，那是 `main`（以及内部的整合策展器）。

| 工具 | 风险 | 用途 | 来源 |
| --- | --- | --- | --- |
| `memory_write` | medium | 将一个 Markdown 记忆文件写入存储。可选的 `description`（一行索引摘要）和 `type`（`user` / `project` / `procedural` / `reference`）由工具自行组装为 frontmatter 块存储。 | `noeta/builtins/memory/impl/store.py` |
| `memory_read` | low | 按需读取已存储记忆的完整文本。 | `noeta/builtins/memory/impl/store.py` |
| `memory_search` | low | 对名称与全文做大小写不敏感的子串匹配，返回 grep 风格摘录（每条记忆最多 3 行，最多 10 条记忆；命中更多时以 `truncated` 标志说明）。 | `noeta/builtins/memory/impl/store.py` |
| `memory_archive` | medium | 将过时的记忆退役到存储的 `archive/` 子目录——它从索引、召回和搜索中掉出，但文件绝不删除，因此人工可恢复。 | `noeta/builtins/memory/impl/store.py` |

## 浏览器工具

仅在**两者同时**成立时挂载：agent 激活了 `browser`（`"browser" in AgentSpec.plugins`），并且该会话绑定到一个活的沙箱容器。在官方预设中，那只有 `web` 子 agent——`main` 保持无浏览器并委派给它，于是非沙箱部署的工具集和稳定前缀不受触动。

五个都是 `high` 风险（任何浏览器动作都可能外泄到任意站点），因此除非会话绕过权限，否则它们都经批准路由。

| 工具 | 风险 | 用途 | 来源 |
| --- | --- | --- | --- |
| `browser_navigate` | high | 前往一个 `url`；返回页面快照。 | `noeta/builtins/browser/impl/__init__.py` |
| `browser_click` | high | 点击 `index`（来自快照的编号列表）处的交互元素。 | `noeta/builtins/browser/impl/__init__.py` |
| `browser_type` | high | 在 `index` 处的元素中键入文本。 | `noeta/builtins/browser/impl/__init__.py` |
| `browser_extract` | high | 将当前页面重新读取为一个快照（无参数）。 | `noeta/builtins/browser/impl/__init__.py` |
| `browser_screenshot` | high | 捕获一张 PNG 并将其存储为**工作区 artifact**，返回其 `ContentRef`。它不会作为视觉输入喂给模型。 | `noeta/builtins/browser/impl/__init__.py` |

四个文本工具返回一个*页面快照*：页面文本加上编号的交互元素。那份编号正是 `browser_click` / `browser_type` 所寻址的，所以一个快照必须先于它们。

名称、schema 和描述由 noeta 钉住，而不是由容器 image 钉住——面向模型的契约（因而也是稳定前缀的缓存字节）不能在沙箱更改其自身工具名时漂移。每个工具都委派给一个 `BrowserBackend`，即容器的浏览器线路被钉住的唯一之处。它是一个每会话的工具包，像 fs 包那样注入，而不是一个 MCP 连接器。

## Skill 工具

| 工具 | 风险 | 用途 | 来源 |
| --- | --- | --- | --- |
| `run_skill_script` | high | 通过一个允许列表中的解释器运行活动 skill 捆绑的脚本。仅当 `skills` 插件配置设置了 `allow_skill_scripts` 且某个活动 skill 附带了脚本时才存在。 | `noeta/builtins/skills/impl/script.py` |

## 控制工具

控制工具是面向模型的 schema，它们翻译成引擎决策，而不是翻译成 `Tool.invoke`。每个都是一个自门控的 `control_tool` 贡献：挂载*就是*启用。

| 工具 | 何时挂载 | 插件 |
| --- | --- | --- |
| `spawn_subagent` | agent 激活 `delegation`（当它有子项时自动推导） | `delegation` |
| `todo_write` | agent 激活 `todo_write` | `todo_write` |
| `ask_user_question` | agent 激活 `ask_user_question` | `ask_user_question` |
| `skill` | agent 激活 `skill_invocation` **且**合并后的 skill 菜单非空 | `skills` |
| `run_workflow` | `HostConfig.workflow_allowed` 开启（且 agent 能委派） | `react` |
| `structured_output` | 设置了 `Options.output_schema` | `react` |

## MCP 工具

当 MCP server 被注册并在每会话中启用时，远程 MCP 工具动态显示为 `mcp__<alias>__<tool>`。见 [ADR：MCP 连接器](https://github.com/initxy/noeta/blob/main/docs/adr/mcp-connectors.md)。

进程内的 SDK MCP server（`create_sdk_mcp_server`）不同：它们的工具保留其**裸**的 `@tool` 名字，没有 `mcp__` 前缀。见[构建自定义工具](../how-to/build-custom-tools.md)。

## 工具风险等级

| 等级 | 含义 |
| --- | --- |
| `low` | 在 agent 自身状态之外无副作用。始终允许。 |
| `medium` | 改变持久状态，但仅限于一个受限目录内（例如记忆存储）。 |
| `high` | 修改文件系统、生成外部进程，或触及活的 Web。须经 `PermissionGuard` 批准。 |
