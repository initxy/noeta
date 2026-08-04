# 插件 manifest 与加载

一份 manifest 是惰性数据：一个名字、一个版本范围、一份可选的配置 schema，以及一串贡献。读取它**不会**导入任何插件代码——正因如此，宿主才能在任何东西跑起来之前，把每一个已安装的插件列举出来并检查冲突。本页讲 manifest 的形状、它可以采用的两种形式、`load_plugins` 如何找到它们，以及一个插件如何打包。

源码：`packages/noeta-sdk/noeta/client/{plugin_manifest,plugin_set,plugins}.py`。

## 分发形式：`[tool.noeta]`

一个已安装的包在 `pyproject.toml` 的 `[tool.noeta]` 下声明它的 manifest，并**把它镜像进 wheel 作为名为 `noeta-plugin.toml` 的包数据**。

```toml
[tool.noeta]
name = "house-style"
requires-noeta = ">=0.4"

[[tool.noeta.contributions]]
surface = "prompt_fragment"
name    = "house-style"
text    = "Answer in at most three sentences."

[[tool.noeta.contributions]]
surface  = "reminder"
ref      = "house_style.reminders:stay_brief"
priority = 500
```

对常规安装，`read_distribution_manifest` 直接从磁盘上读这个文件；对可编辑安装，它回落到 `importlib.util.find_spec`——后者能在不导入的前提下定位一个包。零执行的保证在两种情况下都成立。

`parse_manifest_text` 按优先级接受三种 TOML 形状：`[tool.noeta]`（一个同时携带插件的 `pyproject.toml`）、`[noeta]`，以及裸的顶层键（那份镜像出来的 `noeta-plugin.toml`）。

### manifest 字段

| 字段 | 形状 | 含义 |
| --- | --- | --- |
| `name` | `str`，必填 | 这个插件的身份——加载时的去重键**以及**激活名 |
| `requires-noeta` | `str \| None` | 一个版本范围——加载时会判定（告警；`strict` 下拒绝） |
| `config-schema` | `table \| None` | 面向运维配置的可选 schema |
| `contributions` | 表数组 | 一个贡献一条 |

### 贡献字段

每一条都会变成一个 `ManifestContribution`。

| 键 | 形状 | 含义 |
| --- | --- | --- |
| `surface` | `str`，必填 | 一个已注册的 Surface 名——见 [插件 Surface](plugin-surfaces.md) |
| `name` | `str` | 冲突 / 排序键**以及**列表里的标签；省略时从 `ref` 或 `path` 推导 |
| `ref` | `str \| None` | 一个 `module` 或 `module:qualname` 导入字符串，**只**在执行边界处解析 |
| `path` | `str \| None` | 一个资源路径，供 `skills` 这类纯资源 Surface 使用 |
| `params` | 其余的键 | Surface 专属并原样保留：`reminder` 的 `priority`、`reminder_provider` 的 `seams`、字面量 `prompt_fragment` 的 `text` |

省略 `name` 时，它从 `ref` 的最后一个属性（或模块的最后一段）推导，否则从 `path` 的 basename 推导。`(surface, name)` 在一份 manifest 内必须唯一；重复会抛出 `PluginError` 并指名两条条目。

## 单文件形式：`PluginBuilder`

一个本地 `.py` 插件声明一个模块级的 `PluginBuilder` 并用装饰器标注它的贡献。这个 builder **就是** manifest——这可以接受，因为本地文件本来就要过一道显式的信任门。

```python
# brevity.py — a single-file plugin
from noeta.sdk import PluginBuilder

plugin = PluginBuilder("brevity", requires_noeta=">=0.4")

plugin.prompt_fragment("Answer in at most three sentences.", name="be-brief")

@plugin.reminder(priority=500)
def stay_brief(view):
    return None   # a real reminder returns str | None from the folded view
```

`PluginBuilder(name, *, requires_noeta=None, config_schema=None)` 为每个 Surface 暴露一个方法。每个方法都转发到通用的 `contribute(surface, value, *, name=None, ref=None, path=None, **params)`，后者也覆盖那些没有专用方法的 Surface（`agent`、`content_kind`、`mcp_server`、`skills`、`provider`）。

| 方法 | Surface | 额外参数 |
| --- | --- | --- |
| `tool(fn=None, *, name=None)` | `tool` | —— |
| `reminder(fn=None, *, name=None, priority=0)` | `reminder` | `priority` |
| `reminder_provider(fn=None, *, name=None, seams=())` | `reminder_provider` | `seams` |
| `tool_result_transform(fn=None, *, name=None, priority=0)` | `tool_result_transform` | `priority` |
| `guard(obj=None, *, name=None)` | `guard` | —— |
| `observer(fn=None, *, name=None)` | `observer` | —— |
| `prompt_fragment(text, *, name)` | `prompt_fragment` | `text` |
| `policy(factory=None, *, name=None)` | `policy` | —— |
| `sandbox_provider(obj=None, *, name=None)` | `sandbox_provider` | —— |
| `session_pack(factory=None, *, name=None, priority=0)` | `session_pack` | `priority` |
| `control_tool(factory=None, *, name=None, priority=0)` | `control_tool` | `priority` |

`manifest()` 返回等价的 `PluginManifest`，而被装饰的对象会被缓存下来（`resolved_objects`），因此 loader 解析一个单文件插件的贡献时不需要第二次 import。

## 版本约束：`requires-noeta`

`requires-noeta` 记录这个插件是针对哪个 SDK 版本范围写的，loader 在加载时会拿它跟已安装的 `noeta-sdk` 版本**做实际判定**：

| 结果 | 默认 | `load_plugins(strict=True)` |
| --- | --- | --- |
| 范围被满足 | 静默 | 静默 |
| 范围不被满足 | 抛出 `PluginVersionWarning`，指名插件、它声明的范围和已安装的版本；**插件照常加载** | 抛出 `PluginError`，加载失败 |
| 无法识别的版本表达式 | 抛出 `PluginVersionWarning`："unrecognized requires-noeta specifier … not enforced" | 同样只是告警；**绝不**转成拒绝 |
| `noeta-sdk` 没有安装元数据（仓库内直接跑源码） | 视为满足，只记一条 debug 日志 | 同上 |

默认只告警，是因为一个范围是作者对"我测过什么"的声明，而不是一把锁：拿它来拒绝，会在 SDK 第一次打出插件还没来得及重测的补丁版本时，直接搞坏一个本来能跑的部署。`strict=True` 面向的是把"针对本 SDK 测过"当作发布闸门的部署。

判定器刻意做得很小、不引入依赖——支持 `>=`、`>`、`<=`、`<`、`==`、`!=` 作用在点分版本号上，用逗号做 AND 连接，空格随便写。比这更复杂的东西（`~=`、extras、epoch、预发布标记）一律读作"无法识别"并如实报出来，而不是去猜——一个插件绝不会因为一种 loader 从未承诺理解的写法而被拒绝。

```toml
requires-noeta = ">=0.6,<1.0"
```

从一个已加载的插件上读它：

```python
print(pset.get("memory").manifest.requires_noeta)   # → '>=0.4'
```

## 来源与加载流水线

五个来源，各有自己的门。发现顺序**从不**影响结果——它只影响一条错误信息会指名哪个来源。

| # | 来源 | `load_plugins` 参数 | 门 |
| --- | --- | --- | --- |
| 0 | 内置插件（`noeta.builtins`） | `builtins=True`（默认） | 默认开启；用 `disabled_builtins` 按名字关掉 |
| 1 | entry point（`noeta.plugins` 组） | `entry_points=True` | `enabled` 白名单，在**任何 import 之前**生效 |
| 2 | 显式模块或文件路径 | `modules=[...]` | 调用方指定即视为已授权 |
| 3 | `~/.noeta/plugins/` | `user_dirs=[...]` | 用户自己的机器，受信任 |
| 4 | 工作区的 `.noeta/plugins/` | `workspace_dirs=[...]` | 信任存储；未受信的目录会告警并被跳过 |

对每个候选者：**读 manifest**（对包形式和 `.toml` 形式是零代码执行）→ **在任何 import 之前过 `enabled` 门** → **信任门**（仅来源 4）→ **冲突检查** → **确定性合并**，按 `(plugin, contribution)` 排序。解析各个 `ref` 以及运行每个 Surface 的校验器，只发生在执行边界（`PluginSet.resolve` 及其同伴）。

### `load_plugins(...) -> PluginSet`

```python
load_plugins(
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
    strict=False,                # bool —— 拒绝一个不被满足的 requires-noeta
) -> PluginSet
```

- `builtins=True` 会发现内置目录；传一个 `PluginManifest` 的可迭代对象则注入一组自定义 manifest（那是测试用的接缝）。`disabled_builtins` 按名字丢掉内置项，而这次禁用会被**记录**在返回的集合上，好让宿主在没有任何贡献表达它的地方也能遵守它——禁用 `skills` 正是让 `Client` 彻底不提供 skills 套件的方式。缺席不等于禁用：`builtins=False` 限定的是*被加载的集合*，从不是 SDK 自身的能力。
- `react` **不能**被禁用——`disabled_builtins=["react"]` 会抛出 `PluginError`。它提供每个编译出的 `AgentSpec` 都要钉住的那个默认决策 policy。默认的大脑是可以通过 `policy` Surface *替换*的，但不能被移除。
- `entry_points=True` 通过 `importlib.metadata` 发现 `noeta.plugins` 组；传一个 entry-point 样式对象的可迭代对象（每个暴露 `.name` 和 `.dist`）则改为注入它们。一个所属分发未随包提供 `noeta-plugin.toml` 的 entry point 会大声失败。
- `modules` 的条目可以是一个点分模块、一个 `.py` 文件、一个目录（按来源 3 或来源 4 的方式扫描），或一份 `.toml` manifest。
- `user_dirs` 无条件加载；`workspace_dirs` 只在该目录位于信任存储中时才加载。两者都会扫描携带 `noeta-plugin.toml` 的子目录（零执行）**以及**顶层的 `*.py` 单文件插件（会被执行——那是一个受信目录），并跳过以 `_` 开头的文件。
- `strict=True` 把一个不被满足的 `requires-noeta` 从告警变成 `PluginError`（见上面那张表）。无法解析的版本表达式仍然只是告警。

跨来源的重复插件 **名字**是一个错误，并会指名两个来源。

## `PluginSet`

已加载的、宿主层的集合。它是冻结的；它持有被发现的那些插件以及它们所依据的 Surface registry。每个投影都会记忆自己的解析结果，因此一次构建对每个 `ref` 最多 import 一次。

| 成员 | 返回 | 会执行插件代码吗？ |
| --- | --- | --- |
| `names()` / `__iter__` / `__len__` / `__contains__` / `get(name)` | 列举 | 否 |
| `contributions(surface=None)` | `((plugin_name, ManifestContribution), …)` | **否** |
| `merged()` | `MergedContributions` —— 已查冲突、确定性排序 | **否** |
| `disabled_builtins` | `frozenset[str]` | 否 |
| `resolve()` | 每个贡献，其 `ref` 已 import 并已校验 | **是**——这就是执行边界 |
| `identity_activations(only=None)` | 每个**外部** 插件的身份面贡献 | 是 |
| `activation_transforms(only=None)` | `tool_result_transform` 阶段 | 是 |
| `activation_reminders(only=None)` | compose 时的 `reminder` 渲染 | 是 |
| `activation_reminder_providers(only=None)` | 被记录的 `reminder_provider` | 是 |
| `activation_session_packs(only=None)` | `session_pack` 工厂 | 是 |
| `activation_control_tools(only=None)` | `control_tool` 工厂 | 是 |
| `process_hooks()` | 来自外部插件的 `(guards, observers)`，按 `(plugin, name)` 顺序 | 是 |
| `host_skills_dirs()` | 外部插件的 `skills` 路径，按 `(plugin, name)` 顺序 | 是 |
| `host_mcp_servers()` | 外部插件的 `((alias, plugin, SdkMcpServer), …)` | 是 |

`Client` 在构建期间调用这些激活投影、`process_hooks` 以及那两个 `host_*` 投影，而绝不在某一轮里调用。`host_*` 这一对和 `process_hooks` 都**不**接受 `only=`：它们的 Surface 是进程级或 host 接线的，加载本身就让它们生效。内置插件被排除在它们全部之外——内置项的效果搭乘的是 `compile_options` 按名字处理的那套激活词汇。`only=` 参数把解析限制在某个 agent 实际激活的那些名字上，因此一个被加载但未被激活的插件，它的模块体永远不会运行。

## 信任存储

工作区目录的信任存储是一个 JSON 文件——`{"trusted": [absolute path, …]}`——默认位于 `~/.noeta/trust.json`。只有 `workspace_dirs` 会查它；`user_dirs` 总是被扫描。

| 函数 | 行为 |
| --- | --- |
| `is_trusted(path, store=None) -> bool` | `path` 的规范形式是否已被记录；存储不存在意味着 `False`，而不是一个错误 |
| `grant_trust(path, store=None) -> None` | 记录 `path` 的规范形式（幂等）；存储及其父目录不存在时会创建 |

两侧以相同方式规范化——展开 `~`、取绝对路径、解析符号链接——因此一个路径的写法从不决定信任与否。格式损坏的存储在读取时会抛出 `PluginError`。

```python
from noeta.sdk import grant_trust, load_plugins

grant_trust("./workspace/.noeta/plugins")     # writes ~/.noeta/trust.json
pset = load_plugins(workspace_dirs=["./workspace/.noeta/plugins"])
```

## 失败语义

加载故障是**大声的**，并在启动时让 client 构建失败，绝不会落在会话中途的某一轮。

- 一份糟糕或缺失的 manifest、一个损坏的文件、一个无法 import 的 `ref`、一个缺失的 `ref` 属性，或一个没通过其 Surface 校验器的值，都会抛出指名该插件的 `PluginError`。
- **任何冲突**——两个插件声称同一个键、跨来源的重复插件名、第二个 `policy` 或 `provider`、一个配方已经占用的 `mcp_server` 别名——都会抛出 `PluginError` 并**指名双方。不存在覆盖。**
- 一个**写了但不是整数的 `priority`** 会抛出 `PluginError`，指名插件和那条贡献。按 priority 排序的 Surface 是按整数排的，把它强行折成 0，会把这条贡献放到它作者本想让它待在末尾的那个档位的最前面；完全不写 `priority` 仍然是文档规定的默认值 `0`。
- 一个**未知的激活名**会在编译时抛出 `ValueError`。

有三种不抛异常的跳过，每一种都带告警：

- 一个未受信的 `workspace_dirs` 条目——`UntrustedPluginDirWarning`；
- **在 `enabled` 白名单生效时，一个名字无法被静态读出的单文件插件**——`UnnamedPluginFileWarning`。白名单授权的是*名字*，而一个只有跑起来才知道自己叫什么的文件，压根没有名字可供授权；为了搞清楚它叫什么而去执行它，等于把这道门自己拆了。加一行模块级的 `noeta_plugin_name = "..."`（或者一个 `PluginBuilder("...")` 字面量）就能让这个文件可被门控。**没有**白名单时，这个文件仍然像以前一样被执行并加载。
- 一个不被满足或无法解析的 `requires-noeta`——`PluginVersionWarning`（见上；`strict=True` 会把"不被满足"这一种变成 `PluginError`）。

```python
from noeta.sdk import PluginError, load_plugins

pset = load_plugins(builtins=[m_a, m_b])   # both contribute prompt_fragment "frag"
try:
    pset.merged()
except PluginError as exc:
    print(exc)
# → prompt_fragment 'frag' on surface 'prompt_fragment' is contributed by both
#   plugin 'a' and plugin 'b' — no override
```

## 打包

一个分发的插件会带上同一份数据的两个副本：`pyproject.toml` 里的 `[tool.noeta]`，以及 wheel 内作为包数据的 `noeta-plugin.toml`。让两者保持一致——`python -m noeta.sdk.plugin_check` 会从一个 `PluginBuilder` 推导出 TOML 并在发布时校验它。**没有**控制台脚本。

Noeta 自己的内置项遵循同样的布局，每个能力一个目录，位于 `packages/noeta-sdk/noeta/builtins/<name>/`：`__init__.py` 放零执行的 `MANIFEST`，`impl/` 放代码，而 manifest 的各个 `ref` 指向同级的 impl 模块。manifest 这一层不 import impl 里的任何东西，因此列举一个内置项运行的能力代码为零。

## 下一步

- [插件 Surface](plugin-surfaces.md) —— 一个贡献可以是什么
- [编写插件](../how-to/write-a-plugin.md) —— 面向任务的指南
- [Options](sdk-options.md) —— 这份契约的激活那一半
- [ADR: 插件 contribution bundles](https://github.com/initxy/noeta/blob/main/docs/adr/plugin-contribution-bundles.md) —— 长期的设计论证
