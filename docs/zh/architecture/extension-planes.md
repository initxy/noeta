# 扩展平面

Noeta 里你能扩展的一切都走同一个机制：一个具名的 **Surface**，由插件向它贡献，或者由你通过一个 `Options` 字段手工接上。一共有十六个 Surface，按一条贡献*意味着什么*分成三个**平面** —— 它改变的是 agent 是谁、它如何被挂到一个 host 上，还是 host 提供了什么资源。

本页讲这些平面、填充它们的加载器，以及 Noeta 自己的能力如何走和你一样的路。

<p align="center">
  <img src="../../assets/diagrams/plugin-system.svg" alt="Noeta 插件系统 —— manifest 到加载器到注册表到按 agent 的激活，三个平面与十六个 Surface" width="820">
</p>

## 那条贯穿一切的切分

在讲 Surface 之前，有一条区分贯穿整个设计：**身份 vs 接线**。

- **身份**决定 agent 怎么思考 —— 系统提示、工具集、skills、已激活的插件、决策 Policy。它进入持久记录，并在 fold 时被逐字复现。
- **接线**只是把 agent 挂到一个 host 上 —— provider 实例、工作目录、审批回调、Observer、存储。它被排除在身份之外（`compare=False`），因此换掉它绝不会扰动一份记录。

这条切分是强制的，不是风格问题：记录必须可复现。把两者混在一起，一次重放就会因为有人换了个工作目录而对不上。这也是为什么换 LLM 厂商是免费的 —— 见[切换 Provider](../how-to/swap-providers.md)。

## 三个平面

| 平面 | Surface | 作用域 | 进入 agent 身份？ |
| --- | --- | --- | --- |
| **身份** | `tool`、`agent`、`content_kind`、`prompt_fragment`、`policy`、`control_tool` | 按 agent | **是** |
| **接线** | `guard`、`observer`、`provider`、`reminder_provider`、`reminder`、`tool_result_transform`、`session_pack` | 进程级、host 接线，或按 agent | 否 |
| **host** | `mcp_server`、`skills`、`sandbox_provider` | host 接线 | 否 |

那张表里有两处细节值得展开。

**`guard` 与 `observer` 是进程级的，而且刻意不可退出。** 一旦携带它们的插件被加载，它们就对进程里的每一个 agent 生效，无论那个 agent 有没有激活它。治理属于运维方权限：agent 的作者不应该能靠从一个元组里省掉一个名字来跳过合规拦截或审计。接线平面上的其余一切要么按 agent，要么由 host 接线。

**接线平面上恰好只有两条进程级通道。** 试图注册第三个进程作用域 Surface 的插件会在构造时被拒绝，而不是被悄悄归到那两者之一名下。

每个 Surface 都由一个 `SurfaceSpec` 完整描述：它的平面、激活作用域、校验器、撞车键、排序，以及 —— 对身份类 Surface 而言 —— 一条贡献喂给哪条激活通道。逐 Surface 的完整细节在[插件 Surface 参考](../reference/plugin-surfaces.md)。

## 加载器与 Surface 无关

`load_plugins(...)` 把静态 manifest 读进一个 `PluginSet`，**不导入任何插件代码**。一条贡献的 `ref` 只是一个字符串；它只在客户端构建这条边界上解析。正是这一点让插件在任何一部分运行之前就可以被列举、被审计、被撞车检查。

加载器只查询一个 `SurfaceRegistry` —— 一张 `name → SurfaceSpec` 的映射 —— 别无其他。添加一个 Surface 就是注册一个 `SurfaceSpec`，绝不是去改加载器。host 添加自己的 Surface 的方式是：拿 `standard_registry().copy()`，注册自己的 Surface，然后把那个注册表传给 `load_plugins`；这个 Surface 上的贡献会被同一条流水线校验和撞车检查，然后交给 host，从不进入 agent 身份。

各条贡献按确定性的方式合并，按 `(plugin, name)` 排序，或者在 Surface 声明了整数 `priority` 时按 priority 排序。一次撞车会同时指名双方并失败 —— 没有覆盖，也没有后写者胜出。

## 激活就是身份

加载让一个插件在进程里*可用*。**激活**决定哪些 agent 使用它：`Options.plugins` 和 `AgentDefinition.plugins`，两个名字元组会折进单一的 `AgentSpec.plugins` 身份元组。

功能开关随后通过 `agent_activates(agent, plugin)` 读取这个元组 —— 成员资格*就是*能力。没有第二套开关注册表，也没有事后加装的运行时限制。

一个无法识别的名字会**让编译大声失败**，所以一个拼写错误永远不可能静默地关掉一项能力。`DEFAULT_PLUGINS = ("fs", "web")` 是默认值，且对身份是惰性的，这就是为什么一个裸的 `Options()` 编译出来的字节和它一直以来的结果完全相同。

因为激活就是身份，它会让 KV 缓存前缀翻篇。像规划工具集那样去规划一个 agent 的插件集合 —— 而不是按回合去改。

## 一个 built-in 长什么样

Noeta 自己的能力都是插件，没有任何特权路径。`packages/noeta-sdk/noeta/builtins/<name>/` 下每个 built-in 一个目录：

```
noeta/builtins/memory/
├── __init__.py        # MANIFEST — inert data, zero execution
└── impl/              # the code the manifest's refs point at
```

`__init__.py` 只声明一个 `PluginManifest`，别无其他，所以导入这一层一个实现模块都不会导入。manifest 里的 `ref` 字符串指名 `impl/` 下的兄弟模块，由加载器在客户端构建时通过动态导入解析。`.importlinter` 禁止树中任何地方静态导入 `noeta.builtins`。

因此添加一项第一方能力就是添加一个目录 —— 和发布一个第三方插件是同一件事，只是少了打包那步。编写侧见[编写插件](../how-to/write-a-plugin.md)。

## agent 层

一个 agent 的身份是一个 `AgentSpec`：一个名字加上身份侧的配置 —— 指令、Policy ref、工具、`plugins` 激活元组、`spawnable` 名单。它由 `Options` 编译而来，并被收进一个注册表。这一层位于 runtime 的低处，只依赖 protocol 层，因为一个 agent 是 Task 的一个*类别*，而不是一个网络面。

官方发布四个 agent，每个都有一份刻意收窄的面：

| agent | 角色 | 工具面 | 会委派吗？ |
| --- | --- | --- | --- |
| `main` | 对话控制器 | 完整内置工具 + `todo_write` / `ask_user_question` / `skill_invocation` / `memory` / `mcp` | 会 |
| `general-purpose` | 自包含的执行者 | read / write / edit + shell + web | 不会 —— 叶子 |
| `explore` | 只读侦察 | 只读工具 | 不会 |
| `plan` | 只读规划 | 只读工具 | 不会 —— 产出一份计划 |

那一组之外还存在另外两个身份：`web`，那个只有 `sandbox_browser_options()` 才会放进 main 名单的浏览器子代理；以及 `__consolidation__`，那个由 host 用 `with_consolidation_agent()` 注册的内部记忆整理者。

委派有两种形态。**单个**：父 Task 生成一个子任务、挂起，并在它完成时唤醒。**扇出**：父 Task 生成一组子任务，在一个有界的进程内池上并发运行（`min(8, CPU count)`，可用 `NOETA_MAX_SUBTASK_CONCURRENCY` 覆盖），结果一起返回，每个都与它原本的那次工具调用配对。每个子任务都是一个完整的事件溯源 Task，有自己的日志和 fold，与父 Task 之间只有 `parent_task_id` 这层关系。

## 什么是锁死的

并非所有东西都是 Surface，而这些例外都有原则：

- **Engine 主循环。** 它的控制流只负责路由 Decision；要改变 agent 决定什么，`policy` 这个 Surface 就是为此存在的。
- **Dispatcher / Worker / lease 协议。** host 只能通过 `HostConfig` 调并发和 lease 时序，再无其他 —— 单写入者围栏依赖于它。
- **`ThreeSegmentComposer`。** 不提供整体替换 composer，因为稳定前缀的 KV 缓存可复现性是一条协议级的硬约束。它开放的钩子只在注册表层面且只增不改：一个 `ContentKindSpec`（一个 semi-stable 常驻内容）或一个组装期的 `reminder`（dynamic-suffix 的尾部）。两者都不碰稳定前缀。
- **存储后端。** 经由 `HostConfig` 接上，永远不是插件 Surface，也永远不属于 `AgentSpec` 身份。公共入口是 `noeta.sdk.storage`。

## 接下来去哪

- [包与导入规则](packages.md) —— 让 builtins 这一层只能经由加载器触达的那些导入规则
- [编写插件](../how-to/write-a-plugin.md) —— 编写侧的完整走读
- [插件 Surface 参考](../reference/plugin-surfaces.md) —— 全部十六个，每个一节
- [插件 manifest 参考](../reference/plugin-manifest.md) —— manifest 结构、加载来源与版本约束
