# 编写插件

本指南教你把一组贡献 —— 工具、Guard、reminder、一个 Policy、提示词片段、子 agent —— 打包成一个携带静态 manifest 的**插件**，并在该用它的 agent 上激活它。你需要[你的第一个 agent](../tutorials/first-agent.md) 里的 `Options` 和 `Client`，以及至少一样可以贡献的东西：一个[自定义工具](build-custom-tools.md)或一个 [Guard](../concepts/guard-observer.md)。

## 模型：声明、加载、激活

插件是一个包（或一个单独的 `.py` 文件），携带一份**静态 manifest** —— 一个名字，加上一列*贡献*，每条贡献指名一个 **Surface**（`tool`、`guard`、`reminder`、……）并指向填充它的代码。三步让一个插件开始工作：

1. **声明** —— 写 manifest（一个 `[tool.noeta]` 表，或单文件里的 `PluginBuilder` 调用）。
2. **加载** —— `load_plugins(...)` 把各 manifest 读进一个 `PluginSet`，*不运行任何插件代码*。这是 host 级别的一步：它决定进程里有哪些插件代码可用。
3. **激活** —— 在 `Options.plugins` 里指名某个 agent 要用的插件，并把加载好的集合交给 `Client(options, plugins=...)`。激活是按 agent 的，并且会进入该 agent 的身份。

插件不给引擎**增加任何新能力** —— 它只是填充那十六个扩展 Surface。它给你带来的是可发现性、零执行的清单列举、确定性且做过撞车检查的合并，以及按 agent 的激活。

## 1. 写一个单文件插件

最小的插件就是一个 `.py` 文件，里面有一个模块级的 `PluginBuilder`：

```python
# brevity.py — contributes one prompt fragment.
from noeta.sdk import PluginBuilder

plugin = PluginBuilder("brevity", requires_noeta=">=0.4")

# a static prompt fragment, appended after the agent's system prompt
plugin.prompt_fragment("Answer in at most three sentences.", name="be-brief")
```

`PluginBuilder(name)` 就是 manifest；每个方法记录一条贡献。按路径加载它 —— `builtins=False` 把内置目录排除在外，这样你只看到自己的插件：

```python
from noeta.sdk import load_plugins

pset = load_plugins(builtins=False, modules=["./brevity.py"])
print(pset.names())
print([(c.surface, c.name) for _plugin, c in pset.contributions()])
```

```
('brevity',)
[('prompt_fragment', 'be-brief')]
```

`contributions()` 回答"这个插件贡献了什么"，而**不导入它的代码**。

## 2. 在一个 agent 上激活它

加载让插件*可用*；激活决定哪些 agent 使用它。把它的名字加进 `Options.plugins`，并把加载好的集合传给 `Client`：

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

这个 agent 编译出来的指令末尾会是 *"Answer in at most three sentences."* —— 提示词片段被折进了 agent 的身份。一个**没有**列出 `"brevity"` 的兄弟 agent 拿不到它：功能面跟随激活。

> `DEFAULT_PLUGINS = ("fs", "web")` 是 `Options.plugins` 的默认值。两者对身份都是惰性的 —— 默认工具集无论如何都来自内置工具目录 —— 所以一个裸的 `Options()` 不携带任何额外身份。只有激活了某个有实际效果的东西，你才会往身份里加东西。

## 3. 挑选你的 Surface

`PluginBuilder` 每个 Surface 对应一个方法。完整契约（平面、激活作用域、撞车、排序）在[插件 Surface 参考](../reference/plugin-surfaces.md)里；常用的这些：

| 方法 | 贡献什么 | 跟随激活？ |
| --- | --- | --- |
| `tool(fn)` | 一个 `@tool` 或一个内置工具名 | 是（按 agent） |
| `contribute("agent", defn, name=...)` | 一个子 `AgentDefinition`（走通用路径 —— 没有专门的方法） | 是 |
| `prompt_fragment(text, name=...)` | 追加在提示词之后的文本 | 是 |
| `reminder(fn, priority=...)` | 一个组装期的**纯** reminder | 是 |
| `reminder_provider(fn, seams=[...])` | 一个**被记录的**注入 provider（可以查数据库） | 是 |
| `tool_result_transform(fn, priority=...)` | 记录之前的一个 `ToolResult → ToolResult` 阶段 | 是 |
| `session_pack(factory, priority=...)` | 一条会话构建贡献（工具、后端、常驻内容） | 是 |
| `control_tool(factory, priority=...)` | 一次控制工具挂载 | 是 |
| `policy(factory)` | 该 agent 的决策 Policy（单值） | 是 |
| `guard(obj)` / `observer(fn)` | 治理钩子 | **否 —— 进程级** |
| `contribute("skills", name=..., path="/abs/dir")` | 一个装着 `SKILL.md` 技能包的目录 | 否 —— host 接线，一经加载即生效 |
| `contribute("mcp_server", server, name="<alias>")` | 一个进程内的 `SdkMcpServer`（`create_sdk_mcp_server`） | 否 —— host 接线，一经加载即生效 |
| `sandbox_provider(obj)` | host 选定的一个 sandbox 后端 | 否 —— 由 host 自行解析的清单项 |

> **治理不可退出。** 一个被加载的 `guard` 或 `observer` 对进程里的**每一个** agent 都生效，无论那个 agent 有没有激活该插件 —— agent 的作者不应该能靠省略一次激活来跳过合规拦截或审计。其余一切都跟随按 agent 的激活。

> **host 接线同样不可退出，但理由不同。** `skills` 和 `mcp_server` 属于 host 的目录清单，而不是某个 agent 的功能：把插件加载进来，它们就在这个进程里了。`skills` 的路径必须是**绝对路径**（用 `Path(__file__).parent` 拼出来），而且它的技能包位于最低那一层 —— 用户自己的 `~/.noeta/skills` 和工作区 `.noeta/skills` 里的同名技能依然会把它盖掉。`provider` 和 `sandbox_provider` 则是**由 host 自行解析的清单项**：声明一条只是让它可被发现、可做撞车检查，具体接哪一个由 host 手工决定。

> **技能的 `allowed-tools` 写不了你插件自己的工具。** 那份可识别名单是一次会话能挂载的工具名的静态集合，所以你随插件发布的 `SKILL.md` 里写 `allowed-tools: [Read, my_plugin_tool]`，只会授予 `Read`，其余的会带一条告警被丢弃。要给插件工具加闸，请改用 `guard`。

## 4. 贡献一点有牙齿的东西

一个插件可以填多个 Surface。下面是一个 Guard 插件 —— 属于治理，所以一旦加载就进程级生效：

```python
# block_shell.py
from noeta.sdk import PluginBuilder, ProposedToolCall, VerdictResult

plugin = PluginBuilder("block-shell")


class BlockShellGuard:
    name = "block_shell"
    priority = 25

    def check(self, action, ctx) -> VerdictResult:
        if isinstance(action, ProposedToolCall) and action.call.tool_name == "Bash":
            return VerdictResult.deny("Bash is disabled by block-shell")
        return VerdictResult.allow()


plugin.guard(BlockShellGuard(), name="block_shell")
```

```python
pset = load_plugins(modules=["./block_shell.py"])
client = Client(options, provider=my_provider, workspace_dir=".", plugins=pset)
# the guard now gates Bash for every agent — no activation needed
```

## 5. 接收运维方配置

manifest 可以声明一个 `config-schema` 表，描述该插件期望的运维方配置。host 通过 `HostConfig.plugin_config` 按插件名提供这份配置，它经由 `SessionBuildContext.config("<plugin name>")` 到达一条 `session_pack` 贡献 —— 每个 pack 只解析属于自己的那一项：

```python
# host 这一侧
client = Client(
    options,
    provider=my_provider,
    plugins=pset,
    host_config=HostConfig(plugin_config={"house-style": {"max_words": 120}}),
)
```

```python
# 你的 pack 这一侧
def build_house_style_pack(ctx):
    max_words = ctx.config("house-style").get("max_words")
    if max_words is None:
        return PackContribution()        # 自我关闭：没配置就什么都不贡献
    ...
```

SDK 自己不推导的名字 —— 也就是所有第三方插件 —— 原样透传。对 SDK 自行配置的那四个内置（`fs`、`skills`、`workspace`、`memory`），host 给的键是**逐键覆盖**的，所以覆盖其中一个不会把其余的抹掉。

校验它并在输入有问题时抛错：加载器会把这次抛错包成一个指名你插件的 `PluginError`，因此一次配置错误会**在启动时大声地**让客户端构建失败，而不是拖到会话中途的某一轮。

## 6. 打包分发

自用的话一个单文件就够了。要分发一个插件，让 host 在 `pip install` 之后就能发现它，请发布一个包含三部分的包：

1. `pyproject.toml` 里 **`[tool.noeta]`** 下的 manifest（编写来源，也是 `plugin_check` 校验的对象）；
2. 一份**内容一致的 `noeta-plugin.toml`**，作为 package data 放在包*内部*（`house_style/noeta-plugin.toml`）—— 这是加载器从已安装的发行包上读取的东西，**且不会导入 `house_style`**；
3. 一个位于 SDK 拥有的 `noeta.plugins` 组里的 **entry point**，它仅仅是一个组成员标记 —— 插件的名字来自 manifest，而不是 entry-point 的键。

```toml
# pyproject.toml
[project]
name = "noeta-plugin-house-style"
version = "0.1.0"
dependencies = ["noeta-sdk"]

[project.entry-points."noeta.plugins"]
house-style = "house_style"   # membership marker only; the manifest names the plugin

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
```

```
house_style/
├── __init__.py
├── noeta-plugin.toml        # verbatim copy of [tool.noeta] as a bare-key table
└── tools.py
```

加载器通过基名在发行包的文件清单里找到 `noeta-plugin.toml`（常规安装），或经由 `importlib.util.find_spec` 在包旁边找到它（可编辑安装）。无论哪种方式，`ref` 字符串**只**在执行边界上解析，因此发现与列举永远不会导入 `house_style`。`python -m noeta.sdk.plugin_check`（没有 console script）会从你的声明推导出 TOML，并检查随包发布的 manifest 与之一致，因此两者不可能各走各的。

host 用 `entry_points=True` 发现每一个已安装的插件。已安装的插件是任意代码，所以 server 形态的 host 还会传一份 `enabled` 白名单 —— 只有被批准的插件会加载，其余的**在被导入之前**就被跳过：

```python
pset = load_plugins(entry_points=True, enabled=["house-style"])
```

## 7. 可选：从目录加载，以及信任

本地和开发用的 host 可以把插件丢进一个目录，而不必安装它们。被扫描的目录接受两种形态：带 `noeta-plugin.toml` 的**子目录**（零执行读取），或顶层的**单文件** `.py` 插件（以 `_` 开头的文件会被跳过）。

有两类目录来源，区别在于信任：**`user_dirs`** 无条件扫描（host 自己的 `~/.noeta/plugins`），而 **`workspace_dirs`** —— agent 所操作的检出目录下的 `.noeta/plugins` —— **只有**在它的绝对路径被记录进信任存储时才会被扫描。否则它会被跳过，并伴随一个响亮的 `UntrustedPluginDirWarning`，绝不静默。

```python
from noeta.sdk import grant_trust, load_plugins

grant_trust("./workspace/.noeta/plugins")           # writes ~/.noeta/trust.json
pset = load_plugins(workspace_dirs=["./workspace/.noeta/plugins"])
```

> 目录插件是 host 进程要运行的任意 Python 代码。信任闸门让加载它成为一次刻意的行为，但它**不是**沙箱 —— 只对你本来就愿意从中运行代码的工作区授予信任。server 形态的 host 应该坚持用 entry point 加 `enabled` 白名单，并关掉目录来源。

## 8. 测试它

端到端地加载，并对 `PluginSet` 下断言 —— 全是公共面，不碰任何发现机制的内部。列举是零执行的，所以你可以在不运行插件的情况下对它的贡献下断言：

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

```
2 passed
```

要检验一条贡献的行为，直接构造它并调用 —— 对一个 Guard 来说，就是拿 `ProposedToolCall` 去驱动它的 `check`。`packages/noeta-sdk/noeta/builtins/` 下的各个目录是规范的示范声明：每一个 Surface 都有一个内置的参考实现。

## 激活改变了什么

激活一个插件会改变 agent 的身份 —— 它的工具、子 agent、提示词片段、Policy —— 这会让 KV 缓存前缀翻篇。像规划工具集那样去规划一个 agent 的插件集合，而不是按回合去改。被加载的 `guard` / `observer` 属于进程接线，**不**触碰身份，所以治理可以在不引起前缀翻篇的情况下加入。

## 下一步

- [插件 manifest 参考](../reference/plugin-manifest.md) —— manifest 结构、加载来源与版本约束
- [插件 Surface 参考](../reference/plugin-surfaces.md) —— 全部十六个，每个一节
- [扩展平面](../architecture/extension-planes.md) —— 加载器与各平面如何拼在一起
- [Guard 与 Observer](../concepts/guard-observer.md) —— 插件所打包的治理钩子
