# 编写插件

**目标：** 把一组贡献——工具、Guard、Observer、一个 Provider、content kind、子代理——打包成一个 **插件（Plugin）**，让宿主从单个文件、一个插件目录或一个已安装的 entry point 发现它，并合并进自己的 `Options`。

**开始之前：** 你已经熟悉[你的第一个代理](../tutorials/first-agent.md)里的 `Options` 和 `Client`，并至少了解一种贡献类型——[自定义工具](build-custom-tools.md)或 [Guard](../concepts/guard-observer.md)。

## 什么是插件

插件是一个 Python 模块，它导出唯一一个工厂 `noeta_plugin(api)`。工厂接收一个 `PluginAPI` 累加器，调用它的 `add_*` / `set_*` 方法来记录插件贡献了什么。加载插件就是运行这个工厂；`merge_plugins` 把每个已加载插件的贡献折叠进一个基础 `Options`；你像平常一样编译并运行那个 `Options`。

插件不给引擎**增添任何新能力**——它只是填充 `Options` 已经暴露的扩展面。它是对贡献的*打包*，而不是一种新的贡献类型。它带来的好处是发现能力（发布它，宿主就能找到它）以及一次确定性的、经过冲突检查的合并。

## 单文件插件

最小的插件就是一个导出 `noeta_plugin` 的 `.py` 文件：

```python
# my_plugin.py —— 一个贡献单个 Guard 的插件。
from noeta.protocols.hooks import (
    GuardContext, ProposedAction, ProposedToolCall, VerdictResult,
)


class BlockShellGuard:
    name = "block_shell"
    priority = 25

    def check(self, action: ProposedAction, ctx: GuardContext) -> VerdictResult:
        if isinstance(action, ProposedToolCall) and action.call.tool_name == "shell_run":
            return VerdictResult.deny("shell_run is disabled by the block-shell plugin")
        return VerdictResult.allow()


def noeta_plugin(api) -> None:
    api.add_guard(BlockShellGuard())
```

按路径加载它，并合并进一个 `Options`：

```python
from noeta.sdk import Options, load_plugins, merge_plugins

plugins = load_plugins(modules=["./my_plugin.py"])
options = merge_plugins(Options(system_prompt="You are a helpful agent."), plugins)
```

现在 `options.guards` 里就带着 `BlockShellGuard` 了。像对待任何其他 `Options` 一样，把 `options` 交给 `Client`。

## 工厂与 `PluginAPI`

工厂是唯一必需的导出。它接收一个 `PluginAPI`，记录贡献，然后不返回任何东西。`PluginAPI` **不持有任何活的引擎句柄**——每个方法都是一次纯粹的累加：

| 方法 | 贡献内容 | 落到 |
| --- | --- | --- |
| `add_tool(tool)` | 一个内置工具名字符串，或一个被 `@tool` 装饰的工具 | `Options.allowed_tools` |
| `add_guard(guard)` | 一个 `Guard` | `Options.guards` |
| `add_observer(observer)` | 一个提交后的 `Observer`（一个 callable） | `Options.observers` |
| `set_provider(provider)` | 唯一的 `LLMProvider` | `Options.provider` |
| `add_content_kind(spec)` | 一个 `ContentKindSpec` | `Options.content_channels` |
| `add_agent(name, definition)` | 一个子 `AgentDefinition` | `Options.agents` |
| `add_mcp_server(alias, spec)` | 一个宿主平面的 MCP server spec | 宿主 `HostConfig`（不是 `Options`） |
| `add_skill_dir(path)` | 一个宿主平面的技能目录 | 宿主 `HostConfig`（不是 `Options`） |

校验是急切（eager）的：一个未知的内置工具名、一个非 `ContentKindSpec` 的 content kind、第二次 `set_provider`，或者*同一个插件内*重复的名字，都会在工厂执行时抛出 `PluginError` 并点名该插件。跨插件的冲突留到后面由 `merge_plugins` 捕获。

一个插件可以一次贡献好几样东西：

```python
def noeta_plugin(api) -> None:
    api.add_tool(fetch_weather)             # 一个 @tool（见"构建自定义工具"）
    api.add_agent("researcher", researcher) # 一个 AgentDefinition
    api.add_guard(BlockShellGuard())
```

> **两个平面。** 大多数方法落到 `Options` 上，成为代理身份或接线的一部分。`add_mcp_server` 和 `add_skill_dir` 属于**宿主平面**：它们不进入 `Options`。宿主用 `merged_mcp_servers` / `merged_skill_dirs` 读取它们，并接到自己的 `HostConfig` 里。见[插件参考](../reference/plugins.md)。

## 读取配置

工厂可以声明第二个参数来接收运维方的配置：

```python
def noeta_plugin(api, config) -> None:
    threshold = config.get("threshold", 10)
    api.add_guard(ThresholdGuard(threshold))
```

加载器会检查工厂的签名：只有声明了第二个位置参数的工厂才会被传入配置；只有一个参数的工厂只会收到 API。传配置时以插件名为键：

```python
plugins = load_plugins(
    modules=["./my_plugin.py"],
    config={"my_plugin": {"threshold": 5}},
)
```

在工厂内部校验配置，遇到坏输入就抛异常——加载器会把这次抛出包装成一个点名你插件的 `PluginError`，于是一处配置错误会在**启动时大声地**让 client 构建失败，而不是在会话中途某一轮才炸。第一方的 [`approval-modes`](https://github.com/initxy/noeta/tree/main/examples/plugins/approval-modes) 插件是配置驱动型插件的范例。

插件的名字默认取模块的 stem（`my_plugin.py` 就是 `my_plugin`）。若要固定一个稳定的名字——也就是 `config` 映射和 `enabled` allow-list 使用的键，独立于文件名——在模块顶层设置 `noeta_plugin_name`：

```python
noeta_plugin_name = "block-shell"
```

## 测试它

端到端地加载插件，并对合并后的 `Options` 做断言——全是公开面，不碰发现机制的内部：

```python
from noeta.sdk import Options, load_plugins, merge_plugins

def test_block_shell_lands_a_guard():
    plugins = load_plugins(modules=["./my_plugin.py"])
    options = merge_plugins(Options(system_prompt="root"), plugins)
    names = [getattr(g, "name", None) for g in options.guards]
    assert "block_shell" in names
```

要检验某个贡献的行为，直接构造它并调用它——例如把 guard 建出来，用一批 `ProposedToolCall` 驱动它的 `check`。第一方的 [`tests/test_example_approval_modes.py`](https://github.com/initxy/noeta/blob/main/tests/test_example_approval_modes.py) 展示了两半：对 guard 逐个 verdict 的单元测试，加上一个 `load_plugins` + `merge_plugins` 的端到端测试。

## 用 entry point 打包

单个文件对本地使用已经够了。若要分发一个插件、让宿主在 `pip install` 之后就能发现它，把它做成一个包，在 SDK 所有的 `noeta.plugins` 组里声明一个 entry point：

```toml
# pyproject.toml
[project]
name = "noeta-plugin-block-shell"
version = "0.1.0"
dependencies = ["noeta-sdk"]

[project.entry-points."noeta.plugins"]
block-shell = "block_shell.plugin:noeta_plugin"

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"
```

entry point 的值指向 `noeta_plugin` 工厂。用 `load_plugins(entry_points=True)` 启动的宿主会发现该组里每一个已安装的插件。（短横线不是合法的 Python 模块名，所以即使插件通过 `noeta_plugin_name` 把自己命名为 `block-shell`，可导入的包仍然是 `block_shell`。）

已安装的插件是宿主要运行的任意代码，所以服务端风格的宿主还会传一个显式的 `enabled` allow-list——只有运维批准的插件会加载，其余的在被导入之前就跳过：

```python
plugins = load_plugins(entry_points=True, enabled=["block-shell", "approval-modes"])
```

可参考打包好的第一方示例 [`examples/plugins/approval-modes/pyproject.toml`](https://github.com/initxy/noeta/blob/main/examples/plugins/approval-modes/pyproject.toml)。

## 从目录加载，以及信任

本地和开发用的宿主可以把插件文件丢进一个目录，而不必安装它们。目录来源有两种，区别在于信任：

- **可信目录**（`trusted_dirs=`）——无条件扫描，例如宿主自己的 `~/.noeta/plugins`。
- **工作区目录**（`workspace_dirs=`）——代理所操作的某个检出（checkout）下的 `.noeta/plugins`。因为这个目录是跟着不可信代码一起来的，所以**只有**当它的绝对路径被记录在信任存储里时才会被扫描；否则它会带着一个大声的 `UntrustedPluginDirWarning` 被跳过，绝不静默。

记录一次信任，之后这个目录就会加载：

```python
from noeta.sdk import grant_trust, load_plugins

grant_trust("./workspace/.noeta/plugins")      # 写入 ~/.noeta/trust.json
plugins = load_plugins(workspace_dirs=["./workspace/.noeta/plugins"])
```

被扫描目录里每个顶层 `.py`（以 `_` 开头的文件会被跳过）都必须导出 `noeta_plugin`。

> 目录插件是宿主进程要运行的任意 Python 代码。信任门槛让加载它成为一个有意为之的动作，但它**不是**沙箱——只对你愿意从中运行代码的工作区授予信任。服务端风格的宿主应当坚持用 entry point 加上 `enabled` allow-list，别开启目录来源。

## 合并时会发生什么

`merge_plugins(options, plugins)` 在折叠之前会按 `(插件名, 贡献名)` 对贡献排序，所以编译出的 `AgentSpec` 不随插件加载顺序而变。任何名字冲突——两个插件贡献了同一个 tool / agent / content kind / MCP alias、出现第二个 provider，或者某个名字在基础 `Options` 上已经存在——都会抛出 `PluginError` 并点名**两个**来源。没有覆盖开关：冲突永远是错误。

> 改变插件集合会改变代理的身份（它的工具和子代理），从而翻掉 KV-cache 前缀。这是有意为之的——但请像规划工具集那样规划插件集，而不是每一轮都改。

## 另请参阅

- [插件参考](../reference/plugins.md) — 完整的 `PluginAPI`、`load_plugins`、合并与信任存储 API
- [构建自定义工具](build-custom-tools.md) — 插件所贡献的 `@tool`
- [Guard 与 Observer](../concepts/guard-observer.md) — 插件所打包的 hook 角色
- [ADR：插件贡献包](https://github.com/initxy/noeta/blob/main/docs/adr/plugin-contribution-bundles.md) — 设计理由
