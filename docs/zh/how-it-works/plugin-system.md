# 插件系统

Noeta 里能扩展的东西，全都走同一套机制：插件往某个具名的扩展点（surface）里提供内容，或者你直接通过 `Options` 的字段填进去。Noeta 自己的能力——文件和网页工具、记忆、MCP、沙箱、存储、guard、模型适配器——也都是插件，和你的插件走同一条路。

<NtPlugins lang="zh" />

## 保证什么

- **内置插件没有特权。** 内置插件的加载、校验、合并和第三方插件完全一样。内核从不静态 import `noeta.builtins`，谁这么做，import linter 就让构建失败。
- **加载时不执行代码。** manifest 只是数据，代码要到构建 client 时才导入。
- **不会悄悄覆盖。** 两个插件提供了同名内容，会直接报错并指出双方，没有「后写的赢」。
- **写错名字会报错。** 启用一个不存在的插件名会让编译失败，而不是悄悄少了一项能力。

## 身份配置和接入配置

每个扩展点属于两类之一，这对重放很重要：

- **身份配置（identity）** 决定 agent 怎么想：system prompt、工具、技能、启用的插件、决策 policy。它会被记录，fold 时原样还原。
- **接入配置（wiring）** 只负责把 agent 接到宿主上：模型 provider、工作目录、审批回调、observer、存储。它不算 agent 身份，改了也不会让已有记录对不上。换模型厂商不影响任何东西，原因就在这里。

## 16 个扩展点

| 类别 | 扩展点 | 作用范围 | 算不算 agent 身份 |
| --- | --- | --- | --- |
| 身份 | `tool`、`agent`、`content_kind`、`prompt_fragment`、`policy`、`control_tool` | 每个 agent | 算 |
| 接入 | `guard`、`observer` | 整个进程 | 不算 |
| 接入 | `provider` | 由宿主设置 | 不算 |
| 接入 | `reminder_provider`、`reminder`、`tool_result_transform`、`session_pack` | 每个 agent | 不算 |
| 宿主资源 | `mcp_server`、`skills` | 整个进程，自动接上 | 不算 |
| 宿主资源 | `sandbox_provider` | 由宿主设置 | 不算 |

`guard` 和 `observer` 一旦随插件加载，就对进程里所有 agent 生效。这是有意的：管控归运维方，agent 作者不能靠少写一个名字绕过去。每个扩展点的细节见[扩展点参考](../reference/plugin-surfaces.md)。

## 插件怎么加载

1. `load_plugins(...)` 把 manifest 读成一个 `PluginSet`，不导入任何插件代码。每项内容的 `ref` 只是个字符串。
2. 每项内容按所属扩展点的 `SurfaceSpec` 校验并检查冲突。合并顺序是确定的：按 `(plugin, name)` 排，或在扩展点支持时按 `priority` 排。
3. 构建 client 时，`PluginSet.resolve()` 才按 `ref` 导入代码。
4. **启用**决定哪个 agent 用哪些插件：所有按 agent 生效的扩展点，不管是身份类还是接入类，都由 `Options.plugins` 和 `AgentDefinition.plugins` 决定；其中只有身份类会并入 agent 的身份。默认是 `DEFAULT_PLUGINS = ("fs", "web")`。`guard` 和 `observer` 则对整个进程生效；`provider` 和 `sandbox_provider` 由宿主程序挑选；插件提供的 `mcp_server` 和 `skills` 会自动接上。

加载器本身不认识任何具体扩展点，只查一个 `SurfaceRegistry`。宿主想加自己的扩展点，就复制一份 `standard_registry()`，再注册一个新的 `SurfaceSpec`。

一个内置插件就是 `packages/noeta-sdk/noeta/builtins/<name>/` 下的一个目录：`__init__.py` 里只有 `PluginManifest`，`impl/` 里是 manifest 的 `ref` 指向的代码。`noeta-sdk` 自带 18 个。

## 哪些不开放

| 不开放的部分 | 原因 | 可以改的地方 |
| --- | --- | --- |
| Engine 主循环 | 它只负责分派决策 | `policy` 扩展点 |
| dispatcher、worker、租约协议 | 「只有一个写入方」靠它保证 | `HostConfig` 里的并发数和租约时长 |
| 上下文组装器 | 缓存住的 prompt 开头必须可复现 | 加一个 `content_kind` 或 `reminder` |
| 存储后端 | 属于宿主接入，不算 agent 身份 | `HostConfig`，经由 `noeta.sdk.storage` |

## 对你意味着什么

- 内置插件能做的事，你的插件也能用同样的方式做。
- 启用插件会改变身份，也就会改变缓存住的 prompt 开头；一个 agent 用哪些插件要一次定好，不要每轮换。
- 先在 `Options.allowed_tools` 里放一个 `@tool` 函数；想把工具、提示词和 guard 打成一个包一起发布时，再写插件。

设计记录：
[library SDK architecture](https://github.com/initxy/noeta/blob/main/docs/adr/library-sdk-architecture.md) ·
[plugin contribution bundles](https://github.com/initxy/noeta/blob/main/docs/adr/plugin-contribution-bundles.md) ·
[package layout](https://github.com/initxy/noeta/blob/main/docs/adr/package-layout.md)

## 下一步

- [写插件](../guides/plugins.md)：从头写一个插件。
- [插件 manifest 参考](../reference/plugin-manifest.md)：manifest 的格式和加载来源。
- [扩展点参考](../reference/plugin-surfaces.md)：16 个扩展点逐一说明。
