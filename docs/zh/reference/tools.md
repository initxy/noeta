# 内置工具

本页是"一个 agent 开箱即可调用的一切"的目录：每个工具做什么、它在风险上让你付出什么代价，以及它究竟要满足什么条件才会出现在模型的工具列表里。

工具名是 provider 安全的 `snake_case`，也是模型调用时使用的确切字符串。每个工具都携带一个 `risk_level`，由它决定一次调用是否需要审批。

一个裸的 `Options()`——也就是 `allowed_tools=None`——会挂载**十个**工具：`fs` 包（`Read`、`Glob`、`Grep`、`Edit`、`Write`、`Bash`、`BashOutput`、`KillShell`）和 `web` 包（`WebFetch`、`WebSearch`）。

```python
from noeta.sdk import Options
options = Options(system_prompt="…")          # allowed_tools defaults to None
# the agent sees: Read, Glob, Grep, Edit, Write,
#                 Bash, BashOutput, KillShell, WebFetch, WebSearch
```

其中十个无需任何配置；`WebSearch` 需要一个 API key。本页其余的一切都在别处被门控——memory 和 browser 在 agent 激活上，`open_app` 在宿主接线的网关上，`run_skill_script` 在 `skills` 的插件配置上，MCP 在每会话的注册上。

## 文件系统工具

由 `fs` 内置插件的 manifest 声明（`packages/noeta-sdk/noeta/builtins/fs/__init__.py`）。

| 工具 | 风险 | 做什么 | 源码 |
| --- | --- | --- | --- |
| `Read` | low | 读一个文件（UTF-8），可按行用 `offset` / `limit` 切片。完整正文总是作为一个 artifact ref 卸载出去。**读取不受围栏限制**——见下文。 | `noeta/builtins/fs/impl/read.py` |
| `Glob` | low | 在 `path` 下匹配一个 glob 模式（`**` 递归），返回匹配到的路径，已排序并有上限。 | `noeta/builtins/fs/impl/read.py` |
| `Grep` | low | 用 Python `re` 正则做内容搜索，按 `path` 限定范围、按 `Glob` 过滤。 | `noeta/builtins/fs/impl/read.py` |
| `Edit` | high | 在一个已存在的文件里替换一个精确的 `old` 子串；`replace_all` 把"唯一匹配"切换成"每一处"。 | `noeta/builtins/fs/impl/edit.py` |
| `Write` | high | 写一个文件——新建它，或覆盖一个本会话内已经 `Read` 过的文件。父目录必须存在；`content` 上限 64 KB。 | `noeta/builtins/fs/impl/edit.py` |
| `Bash` | high | 在工作区里运行一条命令；`run_in_background` 让它脱离并返回一个 `job_id`。 | `noeta/builtins/fs/impl/shell.py` |
| `BashOutput` | low | 读取一个后台作业的状态（`running` / `exited`）、退出码和一份新的输出快照。 | `noeta/builtins/fs/impl/shell.py` |
| `KillShell` | high | 停掉一个你启动的后台作业（SIGTERM，宽限期后 SIGKILL）。 | `noeta/builtins/fs/impl/shell.py` |

当 `HostConfig.write_mode` 为 `"dry_run"`（默认）时，三个写工具只暂存一份提议的 diff，而不碰磁盘；`"apply"` 才执行真实写入。

### 读取不受围栏限制

工作区根围住的是**写入**。对 `Read`、`Glob` 和 `Grep`，它只锚定*相对*路径：一个绝对路径指向哪里就读哪里——隔壁的一份 checkout、某个 skill 包捆绑的参考资料，任何服务器进程能读到的东西。这是刻意的（一个 agent 本来就经常需要读它工作区之外的东西），也正因如此，真正要紧的边界是**进程自身的**文件权限，而不是工作区根。一个不能暴露某条路径的部署，就不该以一个能读到它的用户身份来跑这个 agent。

写入才是受围栏的那一半：`Write` / `Edit` 在工作区根之内解析。`HostConfig.write_roots` 逐次调用地回答"这个任务可以写到它工作区之外的这里吗？"；没有 resolver 时，一次工作区外的写入直接失败。`Write` 还额外遵守一个可选的、构造时绑定的工作区相对 `allowed_path_globs` 白名单（为空即不限制）；`Edit` 忽略它。

### Shell 模式

`ShellMode`（`noeta/runtime/shell_policy.py`）在这个包被构建时绑定：

| 模式 | 效果 |
| --- | --- |
| `OFF` | `Bash` 根本不在这个包里。 |
| `ALLOWLIST` | 默认。只有下面这份结构化白名单能通过，且仅限 argv。 |
| `ARBITRARY` | 任何不含 shell 元字符的命令都经由 bash 运行。 |

在 `ALLOWLIST` 下，以下 argv 模式可以通过（`noeta/builtins/fs/impl/shell_rules.py`）：

- `git status` / `git diff`
- `pytest` / `uv run pytest`
- `npm test` / `pnpm test`
- `Grep` / `rg` / `find` / `ls` —— 只读的搜索与列举，因此一个处于 ALLOWLIST 模式、又没有自己的 `Grep` / `Glob` 工具的 agent 仍然能搜索工作区。它们的校验器会拒绝那些会调起另一个程序或改动文件系统的参数。

宿主配置可以追加更多规则（`{"program": …, "subcommand": …}`）；内置的那些始终保留。运维配置的规则比精心挑选的内置项更宽松：它的意思是"这个程序可以运行"，接受任何通过了元字符扫描的尾部参数。

工作区也可以在 `<workspace>/.noeta/shell-allowlist.json` 里带上自己的规则——同样的 JSON 形状，写成一个列表。这个文件属于**仓库内容**，因此要过工作区信任这一关：只有当工作区路径被记录进插件信任存储（`grant_trust`，`~/.noeta/trust.json`，也就是给工作区插件目录和工作区 skill 层把关的那个存储），宿主才会加载它。在一个未受信的工作区里，这个文件不贡献任何规则，宿主会按工作区各警告一次（`UntrustedProjectShellAllowlistWarning`），并在消息里点出是哪个文件。想无条件加载就设 `SdkHost(project_shell_allowlist_trust="open")`，想换一个信任存储就设 `SdkHost(trust_store=…)`。在 `bypassPermissions` 下逐次调用的门本来就不设，所以这个文件根本不会被读。

Shell 元字符（`|`、`;`、`&&`、`>`、…）在分词之前就被拒绝。这是**路径包含加白名单，不是一个进程 sandbox**——`Bash` 是在受信任的工作区里派生外部程序。

## Web 工具

由 `web` 内置插件的 manifest 声明。

| 工具 | 风险 | 做什么 | 源码 |
| --- | --- | --- | --- |
| `WebFetch` | low | 抓取一个公开网页、渲染成 Markdown，再用一次辅助模型调用（`Options.webfetch_model`，默认落在会话主模型上）针对调用方的 `prompt` 作答——主模型读到的是答案，不是原始页面。HTTP 自动升级为 HTTPS，跨主机重定向不跟随而是返回给模型，抓取结果按 URL 缓存 15 分钟。每份结果的第一行都写明这是外部网页内容。始终可用。 | `noeta/builtins/web/impl/fetch.py` |
| `WebSearch` | low | 执行一次网络搜索并把排序后的结果作为 Markdown 返回。**只在设置了 `NOETA_WEB_SEARCH_API_KEY` 时挂载。** | `noeta/builtins/web/impl/search.py` |

### WebFetch 能打到哪里

`WebFetch` 的 URL 完全由模型填，而这个工具是 `low` 风险，静态审批集合永远拦不到它。所以它改成按**每次调用**拦，拦的是主机。

**升级须知。** `WebFetch` 指到哪就能打到哪——公网、内网、回送地址都一样。在会拦的权限模式下，`webfetch_allowed_hosts` 之外的主机需要审批。唯一会被直接拒的是这个工具本来就不抓的 scheme：`file:`、`gopher:` 之类会以「WebFetch fetches http(s) URLs only」失败，这跟主机无关。

**没列进名单的主机要人点头。** `HostConfig.webfetch_allowed_hosts` 列出哪些主机可以不问人直接抓。只有两种写法：

| 条目 | 匹配什么 |
| --- | --- |
| `example.com` | 就这一个主机 |
| `*.example.com` | 它的子域，任意层级——`a.example.com`、`a.b.example.com`——但**不包括** `example.com` 本身 |

要连顶级域名本身一起放行，就两条都写上。写错了（带 scheme、带路径、带端口、带用户名、`*` 出现在开头 `*.` 之外的位置）会在构造 `HostConfig` 时直接报错，而不是悄悄谁也匹配不上。匹配用的是 URL 里真正的主机，小写并做过 IDNA 归一：`https://example.com@evil.test/` 按 `evil.test` 判，`allowed.com` 也绝不会把 `notallowed.com` 一起放过去。

| 权限模式 | 名单外的主机 | 名单内的主机 |
| --- | --- | --- |
| `default` | 逐次调用请求审批 | 直接抓，不打扰 |
| `acceptEdits` | 逐次调用请求审批 | 直接抓，不打扰 |
| `bypassPermissions` | 直接抓，不打扰 | 直接抓，不打扰 |

这道闸是一个逐次调用的谓词，跟 `Bash` 处理白名单外命令的形状一样——所以 `WebFetch` 保持 `risk_level="low"`，名单内的主机不会弹窗，而审批照常走 `ToolCallApprovalRequested` → `approve` / `deny` 这条老路（`Options.can_use_tool` 也一样能接管）。跨主机重定向是交还给模型自己重发一次的，那第二次调用同样由这个谓词判——重定向没法把一次抓取偷渡到未经批准的主机上。

**这道闸不是什么。** 它只是在遇到陌生主机时问一声人，并不能把 agent 圈在某个网络里。手里握着 `Bash` 的 agent 一条 `curl` 就能打到任何地址，所以 `WebFetch` 自己不拦任何地址——真要有出网边界，就在网络层或者 sandbox 容器里做，那一层顺带也管住了 shell。

## App 工具

| 工具 | 风险 | 做什么 | 源码 |
| --- | --- | --- | --- |
| `open_app` | low | 通过宿主的预览网关发布一个工作区 HTML 应用。只在宿主接上了 `HostConfig.app_gateway` 时挂载。 | `noeta/builtins/app/impl/__init__.py` |

## 记忆工具

只在 agent 激活了 `memory` 时挂载。在官方 preset 中，那就是 `main`（以及内部的整理策展员）。

| 工具 | 风险 | 做什么 | 源码 |
| --- | --- | --- | --- |
| `memory_write` | medium | 向存储写入一个 Markdown 记忆文件。可选的 `description`（一行索引摘要）、`type`（`user` / `project` / `procedural` / `reference`）和 `keywords`（逗号分隔的检索别名——跨语言召回的桥梁）会作为一个 frontmatter 块存下来，这个块由工具自己组装，并在磁盘上已有的 fence 与文本自带的 fence 之上逐字段合并（重写时没提到的字段保留——curator 维护的 `keywords` 不会被只改正文的重写抹掉；键写成空值即删除）；工具还会盖上 `created` / `updated` 日期和一条 `source_task` 账本回执，且以新名字写入时会报告相似的既有记忆，让模型合并而不是重复。 | `noeta/builtins/memory/impl/store.py` |
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
| `browser_screenshot` | high | 截取一张 PNG，作为 artifact 存进 `ContentStore` 并返回它的 `ContentRef`——工作区里不会多出文件。它不会作为视觉输入喂给模型。 | `noeta/builtins/browser/impl/__init__.py` |

四个文本类工具返回一份*页面快照*：页面文本加上编号的可交互元素。`browser_click` / `browser_type` 寻址的正是那套编号，因此必须先有一份快照。

名字、schema 和描述由 noeta 钉死，而不是由容器镜像决定——当 sandbox 改动自己的工具名时，面向模型的契约（因而也包括稳定前缀的缓存字节）绝不能跟着漂移。每个工具都委托给一个 `BrowserBackend`，那是容器浏览器线上协议被钉住的唯一地方。它是一个像 fs 包那样注入的每会话工具包，不是一个 MCP 连接器。

## Skill 工具

| 工具 | 风险 | 做什么 | 源码 |
| --- | --- | --- | --- |
| `run_skill_script` | high | 经由一个白名单解释器运行某个活跃 skill 捆绑的脚本。只在 `skills` 的插件配置设置了 `allow_skill_scripts`、且某个活跃 skill 确实带了脚本时才存在。 | `noeta/builtins/skills/impl/script.py` |

## Control tool

Control tool 是面向模型的 schema，它翻译成 engine 决策，而不是一次 `Tool.invoke`。每一个都是一条会自我门控的 `control_tool` 贡献：挂载*本身*就是启用。

下表的**激活名**是写进 `Options.plugins` 的那个名字，跟第一列给模型看的工具名不是一回事；写错会在构建 client 时直接抛 `ValueError`，错误信息里会把合法的名字都列出来。

| 工具 | 何时挂载 | 插件 |
| --- | --- | --- |
| `Task` | agent 激活了 `delegation`（有子 agent 时自动推导） | `delegation` |
| `TodoWrite` | agent 激活了 `todo_write` | `todo_write` |
| `AskUserQuestion` | agent 激活了 `ask_user_question` | `ask_user_question` |
| `skill` | agent 激活了 `skill_invocation` **并且**合并后的 skill 菜单非空 | `skills` |
| `run_workflow` | `HostConfig.workflow_allowed` 打开（且该 agent 能委派） | `react` |
| `RecallHistory` | host 接上了压缩 —— 走 `Client` / `query` 时一直是开的，子 agent 也一样；哪怕还没折叠过任何东西，schema 也在 | `react` |
| `structured_output` | 该 agent 是带 per-helper schema 起跑的子任务 / workflow helper（**不是** `Options.output_schema`，那条走 provider 原生约束） | `react` |

`RecallHistory` 把被压缩折叠掉、只在会话开头留下一条摘要的那些原始消息读回来 —— 原文一直留着，摘要只是在 prompt 里顶替它们的位置。会话里生出来的东西（早先那条报错的原话、压缩前讨论过的代码）不落在任何文件上，`Read` 永远找不回来，这个工具可以。结果是只读的渲染，用 `offset` 翻页；当前折叠区间会写在每次调用的结果里，也写在 `collapsed-context` 那条 reminder 里。

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

`Options.permission_mode` 决定哪些等级真的会被门控：`"default"` 门控 `low` 以上的一切，`"acceptEdits"` 豁免 `Edit` / `Write` 这两个编辑类工具，而 `"bypassPermissions"` 什么都不门控。

## 下一步

- [构建自定义工具](../how-to/build-custom-tools.md) —— 用 `@tool` 加上你自己的
- [Options](sdk-options.md) —— `allowed_tools`、`disallowed_tools`、权限模式
- [Guard 与 Observer](../concepts/guard-observer.md) —— 一次调用如何被拒绝或批准
- [插件 Surface](plugin-surfaces.md) —— 一个工具如何经由一个插件抵达某个 agent
