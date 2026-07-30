# 插件参考

插件机制——**在一个 surface registry 之上、由 manifest 声明的贡献包**，并把**宿主级的加载（load）与代理级的激活（activation）**拆成两步。一个插件携带一份列出其贡献的静态 manifest；加载会把这些 manifest 读取（其间不运行任何插件代码）进一个 `PluginSet`；随后某个代理再通过 `Options.plugins` *激活*它要用的插件。下面每个符号都通过 `noeta.sdk` 重新导出；事实来源：`packages/noeta-sdk/noeta/client/{plugin_manifest,surfaces,plugin_set}.py`。

```python
from noeta.sdk import (
    # the manifest + the single-file builder
    PluginManifest, ManifestContribution, PluginBuilder,
    # the surface registry (the generality mechanism)
    SurfaceSpec, SurfaceRegistry, standard_registry,
    # the loader + the loaded set
    load_plugin_set, PluginSet,
    # activation
    PluginActivation, DEFAULT_PLUGINS,
    # trust + errors
    grant_trust, is_trusted, PluginError,
)
```

> `load_plugin_set` 是 `noeta.client.plugin_set.load_plugins` 在 `noeta.sdk` 里的名字（内部函数叫 `load_plugins`；它以 `load_plugin_set` 别名重新导出，以免与将被移除的 0.4.0 `load_plugins` 撞名，见[已移除的 bundle 路径](#the-retired-bundle-path)）。

> 全文不给行号——它们每次编辑都会漂移。模块路径加成员名才是稳定坐标。

这套机制实现了 [SDK 可扩展性重新设计](https://github.com/initxy/noeta/blob/main/docs/implementation-specs/2026-07-28-sdk-extensibility-redesign.md)（文中会内联引用决策编号 `D1`–`D12`）。

## 一屏看懂模型

- 一个**插件（Plugin）**（`D1`）是一个包（或单个 `.py` 文件），携带一份**静态 manifest**：一个 `name`、一个 `requires-noeta` 版本范围、一个可选的 `config-schema`，以及一组**贡献（contribution）**——每条贡献指明一个 **surface**，外加一个 `ref`（import 字符串）或 `path`（资源）。
- 一个 **Surface**（`D2`/`D3`）是一个扩展点——`tool`、`guard`、`policy`、`reminder`……每个 surface 都有一个 `SurfaceSpec`，描述对它的贡献如何校验、如何判冲突、如何合并、如何排序。加载器是**与 surface 无关的（surface-agnostic）**：它只查询 registry、别无其他，所以宿主可以注册自己的 surface。
- **加载（Load）**（`D5`，宿主级）：`load_plugin_set(...) -> PluginSet`——决定进程里有哪些插件*代码*可用。一个 `PluginSet` **无需执行任何插件代码**就能被列出并做冲突检查。
- **激活（Activate）**（`D5`，代理级）：`Options.plugins: list[str]` 和 `AgentDefinition.plugins`——决定*这个代理*使用哪些已加载的插件。激活会进入 `AgentSpec` 身份。`Client(options, plugins=<PluginSet>)` 把两者绑在一起；一个不在已加载集合里的激活名会让构建失败。

## Manifest 格式（`D1`）

manifest 是惰性数据——读取它**不会**导入任何插件代码。它有两种形态。

### 分发形态 —— `[tool.noeta]` / `noeta-plugin.toml`

一个已安装的包在 `pyproject.toml` 的 `[tool.noeta]` 下声明它的 manifest，并**把它作为 package data `noeta-plugin.toml` 镜像进 wheel**，通过分发元数据定位。读取器（`read_distribution_manifest`，`plugin_manifest.py`）在常规安装下直接从磁盘读取那个文件，在 editable 安装下回退到 `importlib.util.find_spec`（它不导入就能定位一个包）——两种情形下零执行保证都成立。

```toml
# pyproject.toml — the plugin's manifest lives under [tool.noeta]
[tool.noeta]
name = "house-style"
requires-noeta = ">=0.4"

[[tool.noeta.contributions]]
surface = "prompt_fragment"
name    = "house-style"
ref     = "house_style:HOUSE_STYLE"     # module:attr import string

[[tool.noeta.contributions]]
surface  = "tool"
ref      = "house_style.tools:LintTool"
```

`parse_manifest_text` 按优先级接受三种 TOML 形态：`[tool.noeta]`（一个同时携带插件的 `pyproject.toml`）、`[noeta]`，以及裸的顶层键（镜像出来的 `noeta-plugin.toml`）。

### Manifest 字段

| 字段 | 形状 | 含义 |
| --- | --- | --- |
| `name` | `str`，必填 | 插件的身份——加载期去重的键，也是激活名 |
| `requires-noeta` | `str \| None` | 一个版本范围（v1 里仅作提示） |
| `config-schema` | `table \| None` | 运维方配置的可选 schema |
| `contributions` | table 数组 | 每条贡献一项 |

每条贡献是一个 `ManifestContribution`（`plugin_manifest.py`）：

| 键 | 形状 | 含义 |
| --- | --- | --- |
| `surface` | `str`，必填 | 一个已注册的 surface 名（见[目录](#surface-catalog-d3)） |
| `name` | `str` | 冲突/排序的键**兼**列出时的标签；省略时从 `ref` / `path` 推导 |
| `ref` | `str \| None` | 一个 `module` 或 `module:qualname` import 字符串——**仅**在执行边界解析 |
| `path` | `str \| None` | 一个资源路径（用于像 `skills` 这类只有资源的 surface） |
| `params` | 其余键 | surface 特定的参数，原样保留（例如 `reminder` 的 `priority`、`reminder_provider` 的 `seams`） |

省略 `name` 时，它从 `ref` 的属性名（或模块的最后一段）推导，否则从 `path` 的 basename 推导。

### 单文件形态 —— `PluginBuilder`

一个本地 `.py` 插件声明一个模块级的 `PluginBuilder`，并用装饰器标注它的贡献；这个 builder **就是** manifest（`D1`）。这是可接受的，因为本地文件反正都要过一道显式的信任门槛。

```python
# brevity.py — a single-file plugin
from noeta.sdk import PluginBuilder

plugin = PluginBuilder("brevity", requires_noeta=">=0.4")

plugin.prompt_fragment("Answer in at most three sentences.", name="be-brief")

@plugin.reminder(priority=500)
def stay_brief(view):
    return None  # a real reminder returns str | None from the folded projection
```

`PluginBuilder(name, *, requires_noeta=None, config_schema=None)` 为每个 surface 暴露一个装饰器/方法——每个都转发到通用的 `contribute(surface, value, *, name=None, ref=None, path=None, **params)`：

| 方法 | Surface | 参数 |
| --- | --- | --- |
| `tool(fn=None, *, name=None)` | `tool` | — |
| `reminder(fn=None, *, name=None, priority=0)` | `reminder` | `priority` |
| `reminder_provider(fn=None, *, name=None, seams=())` | `reminder_provider` | `seams` |
| `tool_result_transform(fn=None, *, name=None, priority=0)` | `tool_result_transform` | `priority` |
| `guard(obj=None, *, name=None)` | `guard` | — |
| `observer(fn=None, *, name=None)` | `observer` | — |
| `prompt_fragment(text, *, name)` | `prompt_fragment` | — |
| `policy(factory=None, *, name=None)` | `policy` | — |
| `sandbox_provider(obj=None, *, name=None)` | `sandbox_provider` | — |
| `session_pack(factory=None, *, name=None, priority=0)` | `session_pack` | `priority` |

`manifest()` 返回等价的 `PluginManifest`；被装饰的对象还会被缓存（`resolved_objects`），于是加载器无需二次导入就能解析单文件插件的贡献。`python -m noeta.sdk.plugin_check`（**没有** console script）在发布时从装饰器推导并校验 TOML。

## Surface 目录（`D3`） {#surface-catalog-d3}

标准目录有十五个 surface（`surfaces.py`，`STANDARD_SURFACES`）。每一行是一个 `SurfaceSpec`：它落在哪个**平面（plane）**上、它的效果如何在各代理间**限定作用域（scope）**（`D6`）、它的**冲突键（collision key）**、它的**合并规则（merge rule）**，以及它的**排序（ordering）**。★ = 本次重新设计新增。

| Surface | Plane | Scope（`D6`） | Collision key | Merge | Ordering | 备注 |
| --- | --- | --- | --- | --- | --- | --- |
| `tool` | identity | per-agent | `name` | append | `(plugin, name)` | 含 tool pack |
| `agent` | identity | per-agent | `name` | append | `(plugin, name)` | 一个 `AgentDefinition` |
| `content_kind` | identity | per-agent | `kind` | append | `(plugin, name)` | 一个 `ContentKindSpec` |
| `prompt_fragment` ★ | identity | per-agent | `name` | append | `(plugin, name)` | 追加在 preset prompt 之后 |
| `policy` ★ | identity | per-agent | **single-valued** | single | — | base + 已激活插件，或两个插件，= 错误 |
| `guard` | wiring | **process** | none | append | `(plugin, name)` | 治理——见[作用域](#effect-scoping-d6) |
| `observer` | wiring | **process** | none | append | `(plugin, name)` | 治理——见[作用域](#effect-scoping-d6) |
| `provider` | wiring | host-wired | **single-valued** | single | — | `Options.provider` 冲突 = 错误 |
| `reminder_provider` ★ | wiring | per-agent | `name` | append | `(plugin, name)` | 记录型注入（track A） |
| `reminder` ★ | wiring | per-agent | `name` | append | **priority** | compose 期、纯函数（track B） |
| `tool_result_transform` ★ | wiring | per-agent | `name` | append | **priority** | 记录前的 ToolRuntime 阶段 |
| `mcp_server` | host | host-wired | `alias` | append | `(plugin, name)` | 可连接的 server spec |
| `skills` | host | host-wired | none | append | `(plugin, name)` | 只有资源（`path`） |
| `sandbox_provider` ★ | host | host-wired | `name` | append | `(plugin, name)` | 宿主从中选一个 |
| `session_pack` ★ | wiring | per-agent | `name` | append | **priority** | 会话构造工厂（microkernel phase 3）：`(SessionBuildContext) -> PackContribution`，内核 builder 的通用循环按优先级带运行 |

- **Collision key** 为 `none` 表示该 surface 从不冲突（guard / observer / 技能目录）。`single-valued` 表示在整个已加载集合里至多一个。
- **Ordering** 为 `priority` 时，先按整数 `priority` 参数排序，同分再按 `(plugin, name)` 打破平手——沿用 guard-observer-hooks 的先例。其余一律按 `(plugin, name)` 排序，所以发现顺序从不改变结果。

宿主自定义的 surface 会扩展这张表（见 [SurfaceRegistry](#surfacespec-surfaceregistry-d2)）。

### 效果作用域（`D6`） {#effect-scoping-d6}

这里有一处刻意为之的不对称——哪些 surface 跟随按代理的激活，哪些是进程级的：

| Surface | 规则 |
| --- | --- |
| `tool` `agent` `content_kind` `prompt_fragment` `policy` `reminder_provider` `reminder` `tool_result_transform` | **跟随按代理的激活**——特性语义：一个没有激活该插件的代理不会得到它们 |
| `guard` `observer` | **一旦加载 ⇒ 对进程内的每个代理都生效。** 治理是运维方的权威；代理作者不得通过省略激活来跳过拦截或审计 |
| `provider` `sandbox_provider` `mcp_server` `skills` | 宿主接线；由宿主选择并绑定，绝不按代理 |

## `SurfaceSpec` / `SurfaceRegistry`（`D2`）

registry 是那套通用化机制——加载器只查询它、别无其他，所以**新增一个 surface 就是注册一个 `SurfaceSpec`，而不是改动加载器**。

```python
@dataclass(frozen=True)
class SurfaceSpec:
    name: str
    plane: "identity" | "wiring" | "host"
    activation_scope: "per-agent" | "process" | "host-wired"
    validator: Callable[[Any], None]   # raises on an illegal contribution value
    collision_key: "name" | "kind" | "alias" | "single-valued" | "none"
    merge_rule: "append" | "single" | "dict-merge"
    ordering: "sorted" | "priority" = "sorted"
```

`validator` 在**已解析**的值上运行（在 `ref` 被导入之后）；列出和 manifest 级的冲突检查从不调用它，所以它们保持零执行。

`standard_registry()` 返回一个以十五个标准 surface 播种的全新 `SurfaceRegistry`。宿主在加载前于一个**副本**上注册额外的 **app-plane** surface——同一套校验 / 冲突 / 排序流水线会原封不动地作用在它们上：

```python
from noeta.sdk import standard_registry, SurfaceSpec, PluginError

def _valid_route(value):
    if not callable(value):
        raise PluginError("http_route must be callable")

reg = standard_registry()                       # a fresh copy
reg.register(SurfaceSpec(
    "http_route", "host", "host-wired", _valid_route, "name", "append",
))
plugins = load_plugin_set(registry=reg, ...)    # the host's surface is live
```

`SurfaceRegistry` 的方法：`register(spec)`（重复的名字会抛错）、`get(name)`、`names()`、`__contains__`、`copy()`。

## 来源与加载流水线（`D4`）

五种来源，每种都有自己的门槛。发现顺序**从不**影响结果（只影响错误归属）。

| # | 来源 | `load_plugin_set` 参数 | 门槛 |
| --- | --- | --- | --- |
| 0 | 内置插件（`noeta.builtins`） | `builtins=True`（默认） | 默认开启；用 `disabled_builtins` 按名禁用 |
| 1 | entry point（`noeta.plugins` 组） | `entry_points=True` | `enabled` allow-list，在**任何导入之前**生效 |
| 2 | 显式的 module / 文件路径 | `modules=[...]` | 调用方指定 = 已授权 |
| 3 | `~/.noeta/plugins/`（或任意目录） | `user_dirs=[...]` | 用户自己的机器 = 受信任 |
| 4 | 工作区 `.noeta/plugins/` | `workspace_dirs=[...]` | 信任存储（不受信任的目录 → 大声警告 + 跳过） |

每个候选的流水线：**读取 manifest**（对包 / `.toml` 形态零执行）→ **`enabled` 门槛，在任何导入之前** → **信任门槛**（仅来源 4）→ **解析 `ref`** → **按 `SurfaceSpec` 校验** → **冲突检查** → 按 `(plugin, contribution)` 排序的**确定性合并**。解析 / 校验只在调用方触及执行边界（`PluginSet.resolve` 及其同类）时才发生；列出和合并只在静态 manifest 上运行。

### `load_plugin_set(...) -> PluginSet`

```python
load_plugin_set(
    *,
    builtins=True,               # bool | Iterable[PluginManifest]
    disabled_builtins=(),        # Iterable[str]
    entry_points=False,          # bool | Iterable[entry-point-like]
    modules=(),                  # Sequence[str] — dotted modules or file/dir/.toml paths
    user_dirs=(),                # Sequence[path] — scanned unconditionally
    workspace_dirs=(),           # Sequence[path] — scanned only when trusted
    enabled=None,                # Iterable[str] | None — allow-list of plugin names
    trust_store=None,            # Path | None — defaults to ~/.noeta/trust.json
    registry=None,               # SurfaceRegistry | None — defaults to standard_registry()
    entry_point_group="noeta.plugins",
) -> PluginSet
```

- `builtins=True` 发现内置目录（`D11`）；传入一个 `PluginManifest` 的可迭代对象可注入一套自定义集合（测试用的 seam）。`disabled_builtins` 按名丢弃内置插件，并且这个禁用会被**记录**在返回的集合上（`PluginSet.disabled_builtins`），这样宿主也能在没有任何 contribution 表达它的地方兑现它——`skills` 不贡献任何 per-agent contribution，所以禁用它正是 `Client` 不再注入 skills kit 的依据（不索引、没有 `skill` 工具、没有 skill content kind）。注意「不在集合里」不等于「被禁用」：`builtins=False` 限定的是*被加载的集合*，从不影响 SDK 自身的能力。
- `react` **不能**被禁用——`disabled_builtins=["react"]` 会抛 `PluginError`。它提供默认的决策 policy，而每个编译出的 `AgentSpec` 都把这个身份钉为 `POLICY_REF ("react", "1")`；一个没有 policy 的 agent 既没有可编译的身份，也没有可 resume 的 parity。默认的大脑是*可替换*的，而不是可移除的：激活一个贡献 `policy` surface 的插件，它的 ref 就会同时接管身份与被接线的 factory。
- `entry_points=True` 通过 `importlib.metadata` 发现 `noeta.plugins` 组；一个由类 entry-point 对象（`.name` + `.dist`）组成的可迭代对象会把它们注入进来。一个分发里没有随附 `noeta-plugin.toml` 的 entry point 会大声失败。
- `modules` 里的条目可以是点分模块（导入它即是授权）、一个 `.py` 文件、一个目录（像来源 3/4 的目录那样扫描），或一个 `.toml` manifest。
- `user_dirs` 无条件加载；`workspace_dirs` 仅当目录被记录在信任存储里时才加载，否则带 `UntrustedPluginDirWarning` 跳过。两者都会扫描携带 `noeta-plugin.toml` 的子目录（零执行）**以及**顶层的 `*.py` 单文件插件（会被执行——一个受信任的目录），并跳过以 `_` 开头的文件。

跨来源的重复插件**名**是一个错误，会点名两个来源。

## `PluginSet`

已加载的、宿主级的集合（`plugin_set.py`）。冻结的；持有发现到的 `LoadedPlugin`，以及它们所依据加载的那个 surface registry。

| 成员 | 返回 | 执行插件代码？ |
| --- | --- | --- |
| `names()` / `__iter__` / `__len__` / `__contains__` / `get(name)` | 列出 | 否 |
| `contributions(surface=None)` | `((plugin_name, ManifestContribution), …)`——每一条贡献，可选地限定一个 surface | **否**（`D5` / acceptance-2） |
| `merged()` | `MergedContributions`——已做冲突检查、确定性排序 | **否** |
| `resolve()` | `(ResolvedContribution, …)`——每条贡献，其 `ref` 已导入并校验 | **是**——执行边界 |
| `identity_activations()` | `dict[str, PluginActivation]`——每个**外部**插件的身份平面贡献（tools / agents / prompt fragments / policy） | 是 |
| `activation_transforms()` | `dict[str, ((priority, name, fn), …)]`——每个外部插件的 `tool_result_transform` 阶段 | 是 |
| `process_hooks()` | `(guards, observers)`——每个**外部**插件的治理 hook，按 `(plugin, name)` 顺序 | 是 |

`contributions()` 就是 acceptance-2 的保证：调用方无需运行插件的任何代码，就能确切看到一个已安装插件贡献了什么。

```python
pset = load_plugin_set()                    # built-ins on
for plugin_name, contribution in pset.contributions("tool"):
    print(plugin_name, contribution.name)   # no plugin body imported

pset.get("memory").manifest.requires_noeta  # ">=0.4"
```

`Client` 在构建期间调用 `identity_activations` / `activation_transforms` / `process_hooks`（绝不在会话中途某一轮）；内置插件被这三者全部排除——它们的特性效果搭乘 compile 按名处理的能力标志（capability-flag）词汇表（见[激活](#activation-d5-d6)），而默认的 guard / observer 栈是引擎自带的。

## 激活（`D5` / `D6`） {#activation-d5-d6}

加载让插件代码*可用*；**激活**决定一个代理使用哪些已加载的插件。激活名存在于 `Options.plugins` 和 `AgentDefinition.plugins` 上，并进入 `AgentSpec` 身份。

```python
from noeta.sdk import Options, Client, load_plugin_set, DEFAULT_PLUGINS

pset = load_plugin_set(modules=["./brevity.py"])   # built-ins + the local plugin

options = Options(
    system_prompt="You are a coding agent.",
    plugins=DEFAULT_PLUGINS + ("memory", "brevity"),   # activate three by name
)

client = Client(options, provider=..., workspace_dir=".", plugins=pset)
```

一个激活名必须是以下之一：

- 一个映射到 `Capabilities` 身份标志（`D5`）的**内置特性 bundle**：`memory`、`browser`、`skill_invocation`、`todo_write`、`ask_user_question`、`mcp`——激活其中之一只会翻转对应的标志、别无其他（`memory=True` 变成 `plugins=["memory"]`）；
- `delegation`——唯一一个既是**结构性**、又可被显式编写的能力。它通常是推导出来的（带 `agents` 的根代理可以委派；扁平子代理不能），而激活它只会把它**打开**：这是给一个子代理授予 spawn 权限的方式，也就是已退役的 `AgentDefinition.capabilities` 从前做的事。`spawnable` 仍然只从 `agents` 字典推导——激活无法指名某个代理；
- 一个**对身份无影响的内置项**，之所以被识别，是为了让打错的名字仍然大声失败、但不产生任何 compile 效果：`fs`、`web`、`skills`、`reminders`、`governance`、`providers`、`presets`、`sandbox`、`workspace`；
- 交给 `Client` 的那个 `PluginSet` 里某个**已加载插件的名字**——它的身份平面贡献（额外的 tool / 子代理 / prompt fragment / policy）会折叠进来。

`DEFAULT_PLUGINS = ("fs", "web")` 是 `Options.plugins` 的默认值；两者都对身份无影响（默认的 11 个工具集仍来自 `BUILTIN_TOOL_CLASSES`），所以一个**裸的 `Options()` 会字节级相同地编译**成重新设计之前的 spec——这就是对等契约（parity contract）。`AgentDefinition.plugins` 默认为 `()`（子代理的工具来自它自己的 `tools` 字段）。

`Capabilities` 作为激活词汇表已退役：`Capabilities(memory=True)` 变成 `plugins=["memory"]`，官方 preset 也以这种方式声明它们的激活集（`presets/__init__.py`）。`Options.capabilities` / `AgentDefinition.capabilities` 这两个编写字段已被**移除**——`plugins=` 是唯一的激活路径。（编译出的 `AgentSpec.capabilities` 仍是身份载体；激活翻转它的标志。）

一个未知的激活名会让编译以 `ValueError` 失败，并点名那个惹事的名字以及它出现的位置（`Options` 或子代理），同时列出内置词汇表和已加载集合——激活前先加载它，或者改正名字。

## 内置插件（`D11`）

noeta 把自己的能力表达为 `noeta/builtins/` 里的内置插件（栈顶那条 band，与 `noeta.presets` 并列）。自 2026-07-29 的 microkernel 迁移起，每个目录同时持有 manifest **和**实现：`__init__.py` 是零执行的 `MANIFEST`（一个 `PluginManifest`，其贡献携带 `ref` 字符串），`impl/` 是代码，`ref` 指向同目录下的 impl 模块。manifest 层不导入任何 impl，所以列出一个内置插件依然运行零能力代码。加载器通过一次**动态**导入（`builtin_manifests()`）触及目录，而 `.importlinter` 里全域生效的 `sdk-core-not-builtins` 契约保证每一条 band——包括内核——都没有通往 `noeta.builtins` 的静态边。

十四个内置插件（`noeta/builtins/` 下一个内置插件一个目录——manifest 声明的权威范例集）：`fs`、`web`、`memory`、`browser`、`app`、`mcp`、`skills`、`react`、`reminders`、`governance`、`providers`、`sandbox`、`presets`、`workspace`。新增一个第一方能力就是在这里加一个目录（只有当确实需要一个全新的 surface 时，才额外注册一个 `SurfaceSpec`）。

## 信任存储

工作区目录的信任存储是一个 JSON 文件——`{"trusted": [abs path, …]}`——默认在 `DEFAULT_TRUST_STORE`（`~/.noeta/trust.json`）。只有 `workspace_dirs` 会查询它；`user_dirs` 总是被扫描。

| 函数 | 签名 | 行为 |
| --- | --- | --- |
| `is_trusted(path, store=None)` | `→ bool` | `path` 的规范形式是否被记录；缺失的存储 ⇒ `False`，绝不报错 |
| `grant_trust(path, store=None)` | `→ None` | 记录 `path` 的规范形式（幂等）；若存储及其父目录不存在则创建 |

两侧用同一套规则规范化路径——展开 `~`、取绝对、解析符号链接——所以路径怎么拼写从不影响信任判定。格式损坏（非 JSON）的存储在读取时会抛 `PluginError`。

```python
from noeta.sdk import grant_trust, load_plugin_set

grant_trust("./workspace/.noeta/plugins")                      # writes ~/.noeta/trust.json
pset = load_plugin_set(workspace_dirs=["./workspace/.noeta/plugins"])
```

## 失败语义

加载故障是**大声的**，会在启动时让 client 构建失败——绝不拖到会话中途某一轮：

- 一份坏掉或缺失的 manifest、一个损坏的文件、一个无法导入的 `ref`、一个缺失的 `ref` 属性，或一个通不过其 surface `validator` 的值，都会抛出 `PluginError` 并点名该插件。
- **任何冲突**——两个插件宣称了同一个键；跨来源的重复插件名；第二个 `policy` / `provider`——都会抛出 `PluginError` 并**点名两个来源。没有覆盖开关。**（与基础 `Options.policy` / `Options.provider` 的冲突由 `compile_options` / `Client` 构建捕获。）
- 一个**未知的激活名**会在 compile 时抛出 `ValueError`。

唯一不抛异常的跳过是一个**不受信任的 `workspace_dirs`** 条目，它会以 `UntrustedPluginDirWarning` 警告并被跳过。

```python
from noeta.sdk import load_plugin_set, PluginError

pset = load_plugin_set(builtins=[m_a, m_b])   # both contribute prompt_fragment "frag"
try:
    pset.merged()
except PluginError as exc:
    #  prompt_fragment 'frag' on surface 'prompt_fragment' is contributed by both
    #  plugin 'a' and plugin 'b' — no override
    ...
```

## 已移除的 bundle 路径 {#the-retired-bundle-path}

0.4.0 的机制——`noeta_plugin(api)` 工厂、`PluginAPI` 累加器、`load_plugins` + `merge_plugins`、`LoadedPlugin`、`PluginContributions`，以及 `merged_mcp_servers` / `merged_skill_dirs`——已从 `noeta.sdk` **移除**，并被本页的 manifest 机制彻底取代（0.4.0 什么都没发布，所以不欠任何兼容性）。只有 manifest 机制会复用的那些基本件留在 `client/plugins.py` 里：信任存储函数（`grant_trust` / `is_trusted`）以及 `PluginError` / `UntrustedPluginDirWarning`。

## 另请参阅

- [编写插件](../how-to/write-a-plugin.md) — 任务导向的指南
- [SDK 参考](sdk.md) — `Options.plugins` 激活，以及 `Client` / `query` 的 `plugins=` 参数
- [SDK 可扩展性重新设计](https://github.com/initxy/noeta/blob/main/docs/implementation-specs/2026-07-28-sdk-extensibility-redesign.md) — 完整的决策记录（`D1`–`D12`）
- [ADR：插件贡献包](https://github.com/initxy/noeta/blob/main/docs/adr/plugin-contribution-bundles.md) — 长期的设计理由
