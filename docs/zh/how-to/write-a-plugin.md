# 编写插件

**目标：** 把一组贡献——工具、Guard、reminder、一个 policy、prompt fragment、子代理——打包成一个携带静态 manifest 的**插件（Plugin）**，然后在应当使用它的那些代理上**激活（activate）**它。可以来自单个文件、一个插件目录，或一个已安装的包。

**开始之前：** 你已经熟悉[你的第一个代理](../tutorials/first-agent.md)里的 `Options` 和 `Client`，并且至少了解一种贡献类型——一个[自定义工具](build-custom-tools.md)或一个 [Guard](../concepts/guard-observer.md)。

## 模型：manifest、load、activate

一个插件是一个包（或单个 `.py` 文件），它携带一份**静态 manifest**——一个名字，加上一组*贡献*，每条贡献指名一个 **surface**（`tool`、`guard`、`reminder`……）并指向填充它的代码。三步就能让一个插件干活：

1. **声明（Declare）**——写下 manifest（一个 `[tool.noeta]` 表，或单文件里的 `PluginBuilder` 调用）。
2. **加载（Load）**——`load_plugins(...)` 把 manifest 读进一个 `PluginSet`，*其间不运行任何插件代码*。这是宿主级的一步：它决定进程里有哪些插件代码可用。
3. **激活（Activate）**——在 `Options.plugins` 里点名一个代理使用的插件，并把已加载的集合交给 `Client(options, plugins=...)`。激活是按代理的，并进入代理的身份。

一个插件不给引擎**增添任何新能力**——它只是填充那十六个扩展 surface。它带给你的是发现能力、一次零执行的列出、一次确定性的、经过冲突检查的合并，以及按代理的激活。

## 单文件插件

最小的插件就是一个 `.py` 文件，里面有一个模块级的 `PluginBuilder`：

```python
# brevity.py — contributes one prompt fragment.
from noeta.sdk import PluginBuilder

plugin = PluginBuilder("brevity", requires_noeta=">=0.4")

# a static prompt fragment, appended after the agent's system prompt
plugin.prompt_fragment("Answer in at most three sentences.", name="be-brief")
```

`PluginBuilder(name)` 就是 manifest；每个方法记录一条贡献。按路径加载它——`builtins=False` 把内置目录挡在外面，于是你只看到自己的插件：

```python
from noeta.sdk import load_plugins

pset = load_plugins(builtins=False, modules=["./brevity.py"])
print(pset.names())               # ('brevity',)
print(pset.contributions())       # every contribution — no plugin code ran
```

`contributions()` **无需导入插件代码**就能回答「这个插件贡献了什么？」。

### 在代理上激活它

加载让插件*可用*；激活决定哪些代理使用它。把它的名字加到 `Options.plugins` 里，并把已加载的集合传给 `Client`：

```python
from noeta.sdk import Options, Client, DEFAULT_PLUGINS

# built-ins on, plus the local plugin
pset = load_plugins(modules=["./brevity.py"])

options = Options(
    system_prompt="You are a coding agent.",
    plugins=DEFAULT_PLUGINS + ("brevity",),   # fs, web, and brevity
)

client = Client(options, provider=my_provider, workspace_dir=".", plugins=pset)
```

代理编译出的指令现在以 *“Answer in at most three sentences.”* 结尾——这个 prompt fragment 被折进了代理的身份。一个**没有**列出 `"brevity"` 的兄弟代理不会得到它：特性 surface 跟随激活。

> `DEFAULT_PLUGINS = ("fs", "web")` 是 `Options.plugins` 的默认值。两者都对身份无影响——默认工具集无论如何都来自内置工具目录——所以一个裸的 `Options()` 不携带任何额外身份。你只有在激活某个有效果的东西时，才会往身份里添东西。

## 你能贡献的那些 surface

`PluginBuilder` 为每个 surface 提供一个方法。完整的契约（plane、激活作用域、冲突、排序）见[插件参考](../reference/plugins.md)；常用的这些：

| 方法 | 贡献 | 跟随激活？ |
| --- | --- | --- |
| `tool(fn)` | 一个 `@tool` 或一个内置工具名 | 是（按代理） |
| `contribute("agent", defn, name=...)` | 一个子 `AgentDefinition`（通用路径——没有专用方法） | 是 |
| `prompt_fragment(text, name=...)` | 追加在 prompt 之后的文本 | 是 |
| `reminder(fn, priority=...)` | 一个 compose 期、**纯函数**的 reminder | 是 |
| `reminder_provider(fn, seams=[...])` | 一个**记录型**注入 provider（可以查询数据库） | 是 |
| `tool_result_transform(fn, priority=...)` | 记录前的一个 `ToolResult → ToolResult` 阶段 | 是 |
| `session_pack(factory, priority=...)` | 一个会话构建贡献（工具、后端、常驻） | 是 |
| `policy(factory)` | 代理的决策 policy（单值） | 是 |
| `guard(obj)` / `observer(fn)` | 治理 hook | **否——进程级** |
| `sandbox_provider(obj)` | 由宿主选择的沙箱后端 | 否——宿主接线 |

> **治理不可退出。** 一个已加载的 `guard` 或 `observer` 对进程内的**每个**代理都生效，无论该代理是否激活了那个插件——代理作者不得通过省略激活来跳过合规拦截或审计。其余一切都跟随按代理的激活。

## 一次贡献好几样

一个插件可以填充好几个 surface。这里是一个 guard 插件——属于治理，所以一旦加载就进程级地生效：

```python
# block_shell.py
from noeta.sdk import PluginBuilder, ProposedToolCall, VerdictResult

plugin = PluginBuilder("block-shell")


class BlockShellGuard:
    name = "block_shell"
    priority = 25

    def check(self, action, ctx) -> VerdictResult:
        if isinstance(action, ProposedToolCall) and action.call.tool_name == "shell_run":
            return VerdictResult.deny("shell_run is disabled by block-shell")
        return VerdictResult.allow()


plugin.guard(BlockShellGuard(), name="block_shell")
```

```python
pset = load_plugins(modules=["./block_shell.py"])
client = Client(options, provider=my_provider, workspace_dir=".", plugins=pset)
# the guard gates shell_run for every agent — no activation needed
```

## 配置

一份 manifest 可以声明一个 `config-schema` 表，描述插件期望的运维方配置。宿主提供的配置通过 `SessionBuildContext.config("<plugin name>")` 到达 `session_pack` 贡献——每个 pack 只解析自己那一条。校验它，遇到坏输入就抛异常：加载器会把这次抛出包装成一个点名你插件的 `PluginError`，于是一处配置错误会**在启动时大声地**让 client 构建失败，而不是拖到会话中途某一轮。

## 打包一个可安装的插件

单个文件对本地使用已经够了。若要分发一个插件、让宿主在 `pip install` 之后就能发现它，把它做成一个包，包含三部分：

1. `pyproject.toml` 里 **`[tool.noeta]`** 下的 manifest（编写来源，也是 `plugin_check` 所校验的对象）；
2. 一个**匹配的 `noeta-plugin.toml`**，作为 package data 随附在包*内部*（`house_style/noeta-plugin.toml`）——这是加载器从已安装分发上读取的东西，**无需导入 `house_style`**；
3. SDK 所有的 `noeta.plugins` 组里的一个 **entry point**，它只是一个组成员身份标记——插件的名字来自 manifest，而非 entry-point 的键。

```toml
# pyproject.toml
[project]
name = "noeta-plugin-house-style"
version = "0.1.0"
dependencies = ["noeta-sdk"]

[project.entry-points."noeta.plugins"]
house-style = "house_style"          # membership marker; the manifest is the source of truth

# the authoring manifest (mirror it verbatim into house_style/noeta-plugin.toml)
[tool.noeta]
name = "house-style"
requires-noeta = ">=0.4"

[[tool.noeta.contributions]]
surface = "prompt_fragment"
name    = "house-style"
ref     = "house_style:HOUSE_STYLE"

[[tool.noeta.contributions]]
surface = "tool"
ref     = "house_style.tools:LintTool"

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"
```

```
house_style/
├── __init__.py
├── noeta-plugin.toml        # verbatim copy of [tool.noeta] as a bare-key table
└── tools.py
```

加载器通过 basename 在分发的文件列表里定位 `noeta-plugin.toml`（常规安装），或通过 `importlib.util.find_spec` 在包旁边定位它（editable 安装）——两种方式下 `ref` 字符串都**仅**在执行边界解析，所以发现和列出从不导入 `house_style`。`python -m noeta.sdk.plugin_check`（没有 console script）会从插件的声明推导 TOML 并检查随附的 manifest 是否匹配，因此它无法与代码漂移脱节。

宿主用 `entry_points=True` 发现每一个已安装的插件。已安装的插件是任意代码，所以服务端风格的宿主还会传一个 `enabled` allow-list——只有获批的插件会加载，其余的在**被导入之前**就跳过：

```python
pset = load_plugins(entry_points=True, enabled=["house-style"])
```

## 从目录加载，以及信任

本地和开发用的宿主可以把插件丢进一个目录，而不必安装它们。一个被扫描的目录接受两类条目：

- 一个带 `noeta-plugin.toml` 的**子目录**（以零执行读取），或
- 一个顶层的**单文件** `.py` 插件（以 `_` 开头的文件会被跳过）。

有两种目录来源，区别在于信任：

- **`user_dirs`**——无条件扫描，例如宿主自己的 `~/.noeta/plugins`。
- **`workspace_dirs`**——代理所操作的某个检出（checkout）下的 `.noeta/plugins`。因为这个目录是跟着不可信代码一起来的，所以**只有**当它的绝对路径被记录在信任存储里时才会被扫描；否则它会带着一个大声的 `UntrustedPluginDirWarning` 被跳过，绝不静默。

```python
from noeta.sdk import grant_trust, load_plugins

grant_trust("./workspace/.noeta/plugins")           # writes ~/.noeta/trust.json
pset = load_plugins(workspace_dirs=["./workspace/.noeta/plugins"])
```

> 目录插件是宿主进程要运行的任意 Python 代码。信任门槛让加载它成为一个有意为之的动作，但它**不是**沙箱——只对你愿意从中运行代码的工作区授予信任。服务端风格的宿主应当坚持用 entry point 加上 `enabled` allow-list，别开启目录来源。

## 测试它

端到端地加载，并对 `PluginSet` 做断言——全是公开面，不碰发现机制的内部。列出是零执行的，所以你可以在不运行插件的情况下对它的贡献做断言：

```python
from noeta.sdk import load_plugins

def test_block_shell_declares_its_guard():
    pset = load_plugins(builtins=False, modules=["./block_shell.py"])
    listed = [(c.surface, c.name) for _plugin, c in pset.contributions()]
    assert ("guard", "block_shell") in listed

def test_guard_is_process_wide():
    pset = load_plugins(builtins=False, modules=["./block_shell.py"])
    guards, _observers = pset.process_hooks()
    assert [type(g).__name__ for g in guards] == ["BlockShellGuard"]
```

要检验某条贡献的行为，直接构造它并调用它——例如对一个 guard，用一批 `ProposedToolCall` 驱动它的 `check`。`packages/noeta-sdk/noeta/builtins/` 下的那些目录（一个内置插件一个目录，各自持有自己的 `MANIFEST`）是一套权威的、已写好的声明：每个 surface 都有一个内置参照。

## 激活会改变什么

激活一个插件会改变代理的身份——它的工具、子代理、prompt fragment、policy——从而翻掉 KV-cache 前缀。请像规划工具集那样规划一个代理的插件集，而不是每一轮都改。一个已加载的 `guard` / `observer` 是进程接线，**不**触碰身份，所以添加治理无需翻掉前缀。

## 另请参阅

- [插件参考](../reference/plugins.md) —— manifest 格式、完整的 surface 目录、各种来源、`PluginSet`，以及信任存储
- [构建自定义工具](build-custom-tools.md) —— 插件所贡献的 `@tool`
- [Guard 与 Observer](../concepts/guard-observer.md) —— 插件所打包的治理 hook
