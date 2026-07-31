# 插件

一个插件是一个包——或者一个单独的 `.py` 文件——它携带一份**静态 manifest**，列出它向 Noeta 贡献了什么。加载一个插件就是读取这些 manifest；它不运行任何插件代码。随后某个 agent 再去*激活*它想要的插件。这两步拆分就是全部要点：宿主决定什么是可用的，agent 决定它用什么。

Noeta 自己的能力也是这样发布的。11 个默认工具、默认的那组 guard、三条 compose 时的 reminder，全都是内置插件，经由与你所写之物完全相同的 loader 解析。（provider 适配器同样随 SDK 发布，但 host 直接从 `noeta.sdk.providers` 构造它们——`providers` 内置插件不向任何 surface 贡献内容。）

<p align="center"><img src="../../assets/diagrams/plugin-system.svg" alt="插件系统：manifest 到 loader 到 registry 再到按 agent 的激活，跨三个平面与十六个 Surface" width="820"></p>

## 三个平面

每一个贡献都落在十六个 **Surface** 之一上，而每个 Surface 又坐落在三个平面之一上。平面决定这个贡献在哪里生效。

| 平面 | Surface | 效果 |
| --- | --- | --- |
| **identity** | `tool`、`agent`、`content_kind`、`prompt_fragment`、`policy`、`control_tool` | 进入被记录的 `AgentSpec`；跟随按 agent 的激活 |
| **wiring** | `guard`、`observer`、`provider`、`reminder_provider`、`reminder`、`tool_result_transform`、`session_pack` | 是行为，不是身份。`guard` 和 `observer` 一经加载即进程级生效——治理属于运维方的权限，不是 agent 作者可以选择开启的东西 |
| **host** | `mcp_server`、`skills`、`sandbox_provider` | 由宿主挑选并绑定；从不按 agent |

每个 Surface 的完整契约以及一个可照做的示例：[插件 Surface](plugin-surfaces.md)。

## 一个最小的插件

两个文件。manifest 声明贡献；模块放代码。

```toml
# pyproject.toml
[tool.noeta]
name = "house-style"
requires-noeta = ">=0.4"

[[tool.noeta.contributions]]
surface = "prompt_fragment"
name    = "house-style"
text    = "Answer in at most three sentences."

[[tool.noeta.contributions]]
surface = "tool"
ref     = "house_style.tools:LintTool"    # module:attr import string
```

加载它，然后在某个 agent 上激活它：

```python
from noeta.sdk import Client, DEFAULT_PLUGINS, Options, load_plugins

pset = load_plugins(entry_points=True)      # built-ins + installed plugins
print(pset.names())
# → ('app', 'ask_user_question', …, 'house-style', …)

options = Options(
    system_prompt="You are a coding agent.",
    plugins=DEFAULT_PLUGINS + ("house-style",),
)
client = Client(options, provider=my_provider, workspace_dir=".", plugins=pset)
```

manifest 的形状、loader 的五个来源、信任门与版本约束：[插件 manifest](plugin-manifest.md)。

## 先加载，再激活

| 步骤 | 在哪里 | 它决定什么 |
| --- | --- | --- |
| **加载** | `load_plugins(...) -> PluginSet`，宿主层 | 这个进程里有哪些插件 *代码*可用 |
| **激活** | `Options.plugins` / `AgentDefinition.plugins`，agent 层 | *这个 agent* 用哪些已加载的插件 |

`Client(options, plugins=<PluginSet>)` 把两者绑在一起。一个不在加载集里的激活名会让构建失败——大声地、在启动时，绝不在某一轮的中途。激活会进入 `AgentSpec` 身份，因此一个多了某个插件的 agent，在记录里就是另一个 agent。完整的激活词汇见 [Options](sdk-options.md#plugin-激活)。

一个 `PluginSet` 是**在不执行任何插件代码的前提下可列举、可查冲突的**：`.contributions()` 和 `.merged()` 只读静态 manifest，而 `.resolve()` 是唯一的 import 边界，在 client 构建时调用一次。

```python
for plugin_name, contribution in pset.contributions("tool"):
    print(plugin_name, contribution.name)   # no plugin body imported
# → fs read
# → fs glob
# → …
```

## 接下来去哪儿

| 页面 | 涵盖 |
| --- | --- |
| [插件 manifest](plugin-manifest.md) | `[tool.noeta]` 各个表、`PluginBuilder`、`load_plugins`、信任存储、`requires-noeta`、打包 |
| [插件 Surface](plugin-surfaces.md) | 全部十六个 Surface，每个一节，附上演示它的那个内置插件 |
| [编写插件](../how-to/write-a-plugin.md) | 面向任务的指南 |
| [扩展平面](../architecture/extension-planes.md) | 这些平面为什么画在现在的位置 |
