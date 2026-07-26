# 插件参考（`noeta.client.plugins`）

插件机制——一批可被发现的、带类型的 `Options` 贡献包，在 `compile_options` 之前确定性地折叠进一个基础 `Options`。下面的每个符号都通过 `noeta.sdk` 重新导出；事实来源：`packages/noeta-sdk/noeta/client/plugins.py`。

```python
from noeta.sdk import (
    PluginAPI, load_plugins, merge_plugins,
    merged_mcp_servers, merged_skill_dirs,
    grant_trust, is_trusted,
    PluginError, LoadedPlugin, PluginContributions, UntrustedPluginDirWarning,
)
```

一个**插件（Plugin）**是导出 `noeta_plugin(api)` 工厂的 Python 模块。工厂把贡献记录到一个 `PluginAPI` 上；加载就是运行工厂；`merge_plugins` 把结果折叠进一个 `Options`。这个机制不给引擎增添任何能力——它只是填充 `Options` 已经暴露的开放扩展面。

> 全文不给行号——它们每次编辑都会漂移。模块路径加成员名才是稳定坐标。

## 两个平面

一个插件的贡献按它们最终落到哪里分成两类：

- **身份平面**——tools、agents、content kinds、provider、guards、observers——折叠进 `merge_plugins` 返回的 `Options`，从而进入 `AgentSpec` 的身份（tools + agents）或它的接线（其余部分）。
- **宿主平面**——MCP server spec 和技能目录——会被校验并做冲突检查，但**不**进入 `Options`（它们没有 `Options` 面）。宿主用 `merged_mcp_servers` / `merged_skill_dirs` 读取它们，并接到自己的 `HostConfig` 里。

## `PluginAPI`

工厂所接收的累加器（`plugins.py`）。一个纯记录器——不持有活的引擎句柄；每个方法追加一条带类型的贡献，并在检查成本低的地方急切校验。一条坏条目、第二个 provider，或者*同一个插件内*重复的名字，都会在工厂执行时抛出 `PluginError` 并点名该插件。

| 方法 | 记录内容 | 冲突键 | 急切校验（抛 `PluginError`） |
| --- | --- | --- | --- |
| `add_tool(tool)` | 一个内置工具名字符串，或一个带 `.ref` 的工具 | 解析出的工具名 | 未知内置名 / 坏 `.ref`；本插件内重复的名字 |
| `add_guard(guard)` | 一个 `Guard` | — | `None` guard |
| `add_observer(observer)` | 一个提交后的 `Observer` | — | 非 callable 的 observer |
| `set_provider(provider)` | 唯一的 `LLMProvider` | （单值） | `None` provider；第二次调用 |
| `add_content_kind(spec)` | 一个 `ContentKindSpec` | `spec.kind` | 非 `ContentKindSpec`；重复的 kind |
| `add_agent(name, definition)` | 一个子 `AgentDefinition` | `name` | 空名字；非 `AgentDefinition`；重复的名字 |
| `add_mcp_server(alias, spec)` | 一个宿主平面的 MCP server spec | `alias` | 空 alias；`None` spec；重复的 alias |
| `add_skill_dir(path)` | 一个宿主平面的技能目录（强制转成绝对 `Path`） | — | 空路径。**不**要求存在——该目录可以之后再准备 |

`add_tool` 会立即把每个条目解析成它的 `ToolRef`，所以用来判冲突的名字在工厂执行时就已经固定。`add_skill_dir` 存一个绝对路径，但不会 stat 它。

## `load_plugins(...)`

从至多三种按需开启的来源发现并调用插件。每种来源都默认关闭，除非提供了对应参数；裸调用 `load_plugins()` 返回 `[]`。

```python
load_plugins(
    *,
    entry_points=False,
    modules=(),
    trusted_dirs=(),
    workspace_dirs=(),
    enabled=None,
    config=None,
    trust_store=None,
    entry_point_group="noeta.plugins",
) -> list[LoadedPlugin]
```

| 参数 | 类型 / 默认值 | 含义 |
| --- | --- | --- |
| `entry_points` | `bool \| Iterable = False` | `True` 通过 `importlib.metadata` 发现 `noeta.plugins` 组；一个由类 entry-point 对象（`.name` + `.load()`）组成的可迭代对象会把它们注入进来（测试用的 seam）；`False` 什么都不发现 |
| `modules` | `Sequence[str] = ()` | 点分模块路径（`importlib.import_module`）或 `.py` 文件路径（按位置加载）；每个都必须导出 `noeta_plugin` |
| `trusted_dirs` | `Sequence = ()` | **无条件**扫描顶层 `*.py` 的目录（以 `_` 开头的文件会跳过） |
| `workspace_dirs` | `Sequence = ()` | **仅**当被记录在信任存储中时才扫描的目录；否则带 `UntrustedPluginDirWarning` 跳过 |
| `enabled` | `Iterable[str] \| None = None` | 插件名的 allow-list；一旦设置，其余每个候选都会在被导入之前跳过 |
| `config` | `Mapping[str, dict] \| None = None` | 插件名 → 配置 dict，作为工厂的第二个参数传入，**仅**传给声明了第二个位置参数的工厂 |
| `trust_store` | `Path \| None = None` | 为 `workspace_dirs` 查询的信任存储；默认是 `DEFAULT_TRUST_STORE`（`~/.noeta/trust.json`） |
| `entry_point_group` | `str = "noeta.plugins"` | 要发现的 entry-point 组 |

按发现顺序返回一个 `list[LoadedPlugin]`（先 entry points，然后 modules，再 trusted dirs，最后 workspace dirs）。这里的顺序**不**影响编译出的 spec——`merge_plugins` 会重新排序。

### 三个来源

1. **Entry points**——打包并已安装的插件。`entry_points=True` 读取 `noeta.plugins` 组；每个 entry point 加载出的对象就是插件的 `noeta_plugin` 工厂。这是服务端风格的来源，与 `enabled` 搭配。
2. **显式的 modules / files**——`modules` 里放点分导入路径或 `.py` 文件路径。这是不安装就能加载插件的仓内方式。
3. **目录**——`trusted_dirs`（无条件）和 `workspace_dirs`（受信任门控）。两者都扫描顶层 `*.py`，跳过以 `_` 开头的文件；每个文件都必须导出 `noeta_plugin`。

### 名字推导与 allow-list

插件的名字是 entry-point 名、模块/文件的 stem，或者模块级的 `noeta_plugin_name` 覆盖。`enabled` 和 `config` 都以这个名字为键。对 `modules` 来说，`enabled` 是在模块被导入之前拿**推导出的候选 stem** 来匹配的（不导入就无法兑现模块级的覆盖）。

### 大声失败

一个坏掉的插件会大声地以 `PluginError` 失败并点名该插件——一次导入错误、缺失的 `noeta_plugin`、非 callable 的工厂、工厂抛出异常，或者在两个来源里发现了重复的插件名。唯一不抛异常的跳过是一个不受信任的 `workspace_dirs` 条目，它会以 `UntrustedPluginDirWarning` 警告。这条规则是有意的：坏插件必须在启动时让 client 构建失败，绝不能拖到会话中途某一轮。

## `merge_plugins(options, plugins) -> Options`

把 `plugins` 折叠进 `options`，返回一个新的 `Options`。

- **确定性排序。** 贡献在合并前按 `(插件名, 贡献名)` 排序，所以编译出的 `AgentSpec` 不随插件加载顺序而变。基础工具先按它们给定的顺序排在前面，然后是排好序的插件工具；guards 和 observers 在基础的之后按排好序的插件顺序追加。
- **工具展开。** 当 `allowed_tools=None` 且没有插件工具时，它保持 `None`（字节级相同的默认值）。当有插件工具时，`None` 的基础会先展开成完整的内置集，于是插件是**添加**工具，而不是悄悄替换掉内置工具。
- **冲突 = 错误。** 任何被两个插件贡献、或已经存在于基础 `options` 上的 tool、agent、content kind 或 MCP alias，以及第二个 provider，都会抛出 `PluginError` 并点名**两个**来源。v1 里没有覆盖开关。
- **只有身份平面**落到返回的 `Options` 上。宿主平面的贡献（MCP spec、技能目录）在这里也会做冲突检查，但要通过下面的访问器读取——它们没有 `Options` 面。

对于一个已经在基础上的名字，冲突来源在错误信息里标为 `<base options>`；插件来源则标为 `plugin '<name>'`。

## 宿主平面访问器

宿主平面的贡献从不进入 `Options`；宿主单独收集它们，并接到自己的 `HostConfig` 里。

### `merged_mcp_servers(plugins) -> dict[str, spec]`

以 `alias → spec` 收集 MCP server spec，按插件名排序。跨插件的 alias 冲突会抛 `PluginError`（和 `merge_plugins` 做的是同一项检查）。把结果接到 `HostConfig.mcp_server_resolver`。

### `merged_skill_dirs(plugins) -> tuple[Path, ...]`

收集技能目录，去重并按 `(插件名, 路径)` 排序；被多个插件贡献的目录只出现一次。把结果接到宿主的技能目录里。

## 信任存储

工作区目录的信任存储是一个 JSON 文件——`{"trusted": [abs path, ...]}`——默认在 `DEFAULT_TRUST_STORE`（`~/.noeta/trust.json`）。只有 `workspace_dirs` 会查询它；`trusted_dirs` 总是被扫描。

| 函数 | 签名 | 行为 |
| --- | --- | --- |
| `is_trusted(path, store=None)` | `→ bool` | `path` 的绝对形式是否被记录；缺失的存储 ⇒ `False`，绝不报错 |
| `grant_trust(path, store=None)` | `→ None` | 记录 `path` 的绝对形式（幂等）；若存储及其父目录不存在则创建 |

两者的 `store` 都默认是 `DEFAULT_TRUST_STORE`。格式损坏（非 JSON）的存储在读取时会抛 `PluginError`。

## 类型与常量

| 类型 | 形状 | 来源 |
| --- | --- | --- |
| `LoadedPlugin` | frozen：`name: str`、`contributions: PluginContributions` | `plugins.py` |
| `PluginContributions` | frozen：`tools`、`guards`、`observers`、`provider`、`content_kinds`、`agents`、`mcp_servers`、`skill_dirs`——单个插件工厂的输出；保留该插件的贡献顺序 | `plugins.py` |
| `PluginError` | `RuntimeError` 子类——每一个加载故障和合并冲突 | `plugins.py` |
| `UntrustedPluginDirWarning` | `UserWarning`——一个不受信任的 `workspace_dirs` 条目被跳过 | `plugins.py` |

`noeta.client.plugins` 上的模块级常量（不通过 `noeta.sdk` 重新导出）：

- `PLUGIN_ENTRY_POINT_GROUP = "noeta.plugins"` —— SDK 所有的运行时平面 entry-point 组。
- `DEFAULT_TRUST_STORE = Path.home() / ".noeta" / "trust.json"` —— 默认的信任存储。

## 另请参阅

- [编写插件](../how-to/write-a-plugin.md) — 任务导向的指南
- [SDK 参考](sdk.md) — 插件折叠进去的那些 `Options` 字段
- [ADR：插件贡献包](https://github.com/initxy/noeta/blob/main/docs/adr/plugin-contribution-bundles.md) — 设计理由（平面、确定性合并、严格的冲突处理）
