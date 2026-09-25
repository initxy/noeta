# 插件清单与加载

插件清单（manifest）是纯数据——名字、版本范围、可选的配置 schema、贡献列表——所以宿主不导入任何插件代码，就能列出并检查所有插件有没有冲突。

源码：`packages/noeta-sdk/noeta/client/{plugin_manifest,plugin_set,plugins}.py`。

## 打包形式：`[tool.noeta]`

在 `pyproject.toml` 里声明，同时把同样的内容作为包数据 `noeta-plugin.toml` 打进 wheel。

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

`parse_manifest_text` 依次接受 `[tool.noeta]`、`[noeta]` 和顶层键三种写法。`read_distribution_manifest` 直接从磁盘读文件，可编辑安装则用 `importlib.util.find_spec` 定位——两种情况都不导入包。

### 清单字段

| 字段 | 类型 | 默认 | 含义 |
| --- | --- | --- | --- |
| `name` | `str` | 必填 | 插件名：去重的依据，也是启用名 |
| `requires-noeta` | `str` | `None` | 支持的 SDK 版本范围，加载时检查 |
| `config-schema` | table | `None` | 运维配置的 schema |
| `contributions` | table 数组 | 空 | 每项一个贡献 |

### 贡献字段

| 键 | 类型 | 默认 | 含义 |
| --- | --- | --- | --- |
| `surface` | `str` | 必填 | 一个已注册的[扩展点](plugin-surfaces.md) |
| `name` | `str` | 自动推出 | 冲突检查和排序用的键；不写时取 `ref` 的最后一段属性名（或模块名末段），再不行取 `path` 的文件名 |
| `ref` | `str` | `None` | `module` 或 `module:qualname`，真正用到时才导入 |
| `path` | `str` | `None` | 纯资源类扩展点（`skills`）用的路径 |
| 其余键 | 任意 | — | 扩展点自己的参数，原样保留：`priority`、`seams`、`text` |

同一份清单里 `(surface, name)` 不能重复，否则抛 `PluginError`。

## 单文件形式：`PluginBuilder`

本地的单个 `.py` 插件在模块顶层声明一个 `PluginBuilder`，它本身就是清单。

```python
# brevity.py
from noeta.sdk import PluginBuilder

plugin = PluginBuilder("brevity", requires_noeta=">=0.4")

plugin.prompt_fragment("Answer in at most three sentences.", name="be-brief")

@plugin.reminder(priority=500)
def stay_brief(view):
    return None   # return str | None from the folded view
```

`PluginBuilder(name, *, requires_noeta=None, config_schema=None)`。每个方法最后都调用 `contribute(surface, value=None, *, name=None, ref=None, path=None, **params)`；`agent`、`content_kind`、`mcp_server`、`skills`、`provider` 没有专门方法，直接用它。

| 方法 | 扩展点 | 额外参数 |
| --- | --- | --- |
| `tool(fn=None, *, name=None)` | `tool` | — |
| `reminder(fn=None, *, name=None, priority=0)` | `reminder` | `priority` |
| `reminder_provider(fn=None, *, name=None, seams=())` | `reminder_provider` | `seams` |
| `tool_result_transform(fn=None, *, name=None, priority=0)` | `tool_result_transform` | `priority` |
| `guard(obj=None, *, name=None)` | `guard` | — |
| `observer(fn=None, *, name=None)` | `observer` | — |
| `prompt_fragment(text, *, name)` | `prompt_fragment` | `text` |
| `policy(factory=None, *, name=None)` | `policy` | — |
| `sandbox_provider(obj=None, *, name=None)` | `sandbox_provider` | — |
| `session_pack(factory=None, *, name=None, priority=0)` | `session_pack` | `priority` |
| `control_tool(factory=None, *, name=None, priority=0)` | `control_tool` | `priority` |

`manifest()` 返回等价的 `PluginManifest`；被装饰的对象缓存在 `resolved_objects` 里，不会再导入一次。

## `requires-noeta`

加载时拿已安装的 `noeta-sdk` 版本来比。

| 结果 | 默认 | `strict=True` |
| --- | --- | --- |
| 满足 | 无输出 | 无输出 |
| 不满足 | `PluginVersionWarning`，插件照常加载 | `PluginError` |
| 写法看不懂 | `PluginVersionWarning`，不检查 | 同左 |
| `noeta-sdk` 没有安装元数据（仓库源码） | 当作满足 | 同左 |

支持 `>=`、`>`、`<=`、`<`、`==`、`!=`，版本号用点分隔，多个条件用逗号连（`">=0.6,<1.0"`）。`~=`、extras、epoch、预发布标记都算看不懂。

## `load_plugins`

```python
load_plugins(
    *,
    builtins=True,               # bool | Iterable[PluginManifest]
    disabled_builtins=(),        # Iterable[str]
    entry_points=False,          # bool | Iterable[entry-point-like]
    modules=(),                  # dotted modules, .py files, dirs, or .toml paths
    user_dirs=(),                # always scanned
    workspace_dirs=(),           # scanned only when trusted
    enabled=None,                # allow-list of plugin names, applied before any import
    trust_store=None,            # default ~/.noeta/trust.json
    registry=None,               # default standard_registry()
    entry_point_group="noeta.plugins",
    strict=False,                # refuse an unsatisfied requires-noeta
) -> PluginSet
```

| # | 来源 | 参数 | 放行条件 |
| --- | --- | --- | --- |
| 0 | 内置插件（`noeta.builtins`） | `builtins=True` | 默认开；用 `disabled_builtins` 按名关掉 |
| 1 | entry point（`noeta.plugins`） | `entry_points=True` | `enabled` 白名单 |
| 2 | 显式指定的模块 / 路径 | `modules=[...]` | 调用方指定即授权 |
| 3 | `~/.noeta/plugins/` | `user_dirs=[...]` | 默认信任 |
| 4 | 工作区 `.noeta/plugins/` | `workspace_dirs=[...]` | 查信任记录；不信任的目录警告后跳过 |

每个候选的处理顺序：读清单 → `enabled` 白名单 → 信任检查（仅来源 4）→ 冲突检查 → 按 `(plugin, contribution)` 排序合并。发现顺序不影响结果。`ref` 到真正用到时才导入和校验。

- `disabled_builtins` 会记在返回的集合上；关掉 `skills` 会让 `Client` 整个不挂技能。`disabled_builtins=["react"]` 会报错——`react` 提供默认决策循环，只能通过 `policy` 扩展点替换，不能去掉。
- entry point 对应的发行包没带 `noeta-plugin.toml` 会直接报错。
- 扫描目录时，带 `noeta-plugin.toml` 的子目录只读不执行，顶层 `*.py` 会被执行；`_` 开头的文件跳过。
- 不同来源出现同名插件会报错，并指出两边的来源。

## `PluginSet`

不可变；每个查询结果都会缓存，所以每个 `ref` 最多导入一次。

| 成员 | 返回 | 是否执行插件代码 |
| --- | --- | --- |
| `names()`、`__iter__`、`__len__`、`__contains__`、`get(name)` | 列表 | 否 |
| `contributions(surface=None)` | `((plugin_name, ManifestContribution), …)` | 否 |
| `merged()` | `MergedContributions`，已查冲突并排序 | 否 |
| `disabled_builtins` | `frozenset[str]` | 否 |
| `resolve()` | 导入并校验全部贡献 | 是 |
| `identity_activations(only=None)` | 外部插件的身份类贡献 | 是 |
| `activation_transforms(only=None)` | `tool_result_transform` | 是 |
| `activation_reminders(only=None)` | `reminder` | 是 |
| `activation_reminder_providers(only=None)` | `reminder_provider` | 是 |
| `activation_session_packs(only=None)` | `session_pack` 工厂 | 是 |
| `activation_control_tools(only=None)` | `control_tool` 工厂 | 是 |
| `process_hooks()` | 外部插件的 `(guards, observers)` | 是 |
| `host_skills_dirs()` | 外部插件的 `skills` 路径 | 是 |
| `host_mcp_servers()` | `((alias, plugin, SdkMcpServer), …)` | 是 |

`Client` 在构建时各调一次。`only=` 只解析有 agent 启用了的插件。内置插件不出现在这些结果里，它们靠启用名生效。

## 信任记录

一个 JSON 文件 `{"trusted": [绝对路径, …]}`，默认在 `~/.noeta/trust.json`。只有 `workspace_dirs`（以及工作区技能、工作区 shell 白名单文件）会查它。

| 函数 | 行为 |
| --- | --- |
| `is_trusted(path, store=None) -> bool` | 规范化后的路径是否已记录；文件不存在返回 `False` |
| `grant_trust(path, store=None) -> None` | 记录规范化后的路径（重复调用无副作用）；文件不存在会创建 |

路径会先规范化（展开 `~`、转绝对路径、解析符号链接）。文件格式不对会抛 `PluginError`。

```python
from noeta.sdk import grant_trust, load_plugins

grant_trust("./workspace/.noeta/plugins")
pset = load_plugins(workspace_dirs=["./workspace/.noeta/plugins"])
```

## 出错时

加载错误在客户端启动构建时就报，不会拖到某一轮对话中间。

| 情况 | 结果 |
| --- | --- |
| 清单缺失或格式错、`ref` 导入失败、值没通过扩展点校验 | `PluginError`，指出插件名 |
| 任何冲突（同一个键、跨来源同名插件、第二个 `policy` / `provider`、`mcp_server` 别名撞车） | `PluginError`，指出双方；不能覆盖 |
| 写了 `priority` 但不是整数 | `PluginError` |
| 未知的启用名 | 编译时 `ValueError` |
| 不受信任的 `workspace_dirs` 目录 | 跳过，`UntrustedPluginDirWarning` |
| 设了 `enabled` 时，单文件插件的名字没法静态读出 | 跳过，`UnnamedPluginFileWarning`；在文件里写 `noeta_plugin_name = "..."` 或字面量 `PluginBuilder("...")` |
| `requires-noeta` 不满足或看不懂 | `PluginVersionWarning` |

## 打包

`[tool.noeta]` 和 wheel 里的 `noeta-plugin.toml` 要保持一致；`python -m noeta.sdk.plugin_check` 能从 `PluginBuilder` 生成 TOML 并校验。内置插件也是同样的结构：`noeta/builtins/<name>/__init__.py` 放 `MANIFEST`，`impl/` 放代码。

## 下一步

- [扩展点](plugin-surfaces.md)——贡献可以是哪些东西
- [写一个插件](../guides/plugins.md)——上手指南
- [插件系统](../how-it-works/plugin-system.md)——扩展点和加载怎么配合
