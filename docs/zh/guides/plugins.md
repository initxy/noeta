# 写一个插件

插件把工具、guard、提醒、提示词片段或子 agent 打成一个包，起一个名字。host 加载一次，每个 agent 自己决定用不用。Noeta 自带的能力也都是用同样方式接进来的内置插件。

## 最小的插件

一个 `.py` 文件，模块里放一个 `PluginBuilder`：

```python
# brevity.py
from noeta.sdk import PluginBuilder

plugin = PluginBuilder("brevity")
plugin.prompt_fragment("Answer in at most three sentences.", name="be-brief")
```

加载它，列出它提供了什么，整个过程不会执行插件代码：

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

## 在 agent 上启用

加载只是让插件在进程里可用，哪个 agent 用它由 `Options.plugins` 决定：

```python
from noeta.sdk import DEFAULT_PLUGINS, Client, Options, load_plugins
from noeta.sdk.providers import AnthropicProvider

pset = load_plugins(modules=["./brevity.py"])        # built-ins + brevity

options = Options(
    system_prompt="You are a coding agent.",
    plugins=DEFAULT_PLUGINS + ("brevity",),          # ("fs", "web", "brevity")
)
client = Client(options, provider=AnthropicProvider(), model="claude-sonnet-5", workspace_dir=".", plugins=pset)
```

- 这个 agent 的指令末尾会多出 "Answer in at most three sentences."，没写 `"brevity"` 的 agent 不受影响。
- `Options.plugins` 里写了没加载的名字，构建 `Client` 时就会报错。
- 启用哪些插件算 agent 身份的一部分，改了会让提示词缓存前缀失效。所以按 agent 定好，别每轮换。

## 选对扩展点

`PluginBuilder` 每个扩展点（surface）有一个对应方法，没有专门方法的用 `contribute(surface, value, name=...)`。每个扩展点的完整规则见[插件扩展点](../reference/plugin-surfaces.md)。

| 你想要 | 方法 | 对谁生效 |
| --- | --- | --- |
| 加一个工具 | `tool(fn)` | 启用了该插件的 agent |
| 加一个子 agent | `contribute("agent", defn, name=...)` | 启用了该插件的 agent |
| 在系统提示词后追加文字 | `prompt_fragment(text, name=...)` | 启用了该插件的 agent |
| 每轮插一段提醒（纯函数） | `reminder(fn, priority=...)` | 启用了该插件的 agent |
| 插一段会记进日志的提醒（可以查数据库） | `reminder_provider(fn, seams=[...])` | 启用了该插件的 agent |
| 工具结果记录前先改写 | `tool_result_transform(fn)` | 启用了该插件的 agent |
| 按任务构建工具或后端，可读配置 | `session_pack(factory)` | 启用了该插件的 agent |
| 替换决策策略 | `policy(factory)` | 启用了该插件的 agent（每个 agent 只能有一个） |
| 拦截或放行动作 | `guard(obj)` | **进程里所有 agent** |
| 旁听事件（审计、指标） | `observer(fn)` | **进程里所有 agent** |
| 附带 `SKILL.md` 技能包 | `contribute("skills", name=..., path="/abs/dir")` | 整个 host，加载即生效 |
| 附带进程内 MCP 服务器 | `contribute("mcp_server", server, name=alias)` | 整个 host，加载即生效 |
| 提供一种沙箱后端 | `sandbox_provider(obj)` | 由 host 选用 |

::: warning guard 和 observer 绕不开
加载进来的 `guard` 或 `observer` 对所有 agent 生效，不管它有没有启用这个插件。这样 agent 作者就没法靠不启用来跳过合规检查或审计。
:::

一个 guard 插件：

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

`load_plugins(modules=["./block_shell.py"])` 就够了，不用启用。

## 读运维配置

host 在 `HostConfig.plugin_config` 里按插件名传配置，`session_pack` 读自己那一份：

```python
host_config = HostConfig(plugin_config={"house-style": {"max_words": 120}})

def build_house_style_pack(ctx):              # your session_pack factory
    max_words = ctx.config("house-style").get("max_words")
    ...
```

拿到不合法的值就直接抛异常：构建 `Client` 时它会变成一个带插件名的 `PluginError`，启动时就能发现。

## 打包发布

想让别人 `pip install` 之后 host 能自动找到你的插件，包里要有三样东西：

1. `pyproject.toml` 里的 `[tool.noeta]` 清单；
2. 包目录里一份内容相同的 `noeta-plugin.toml`，加载器读它，不用 import 你的代码；
3. `noeta.plugins` 组里的一个入口点（插件名以清单为准，和入口点的 key 无关）。

```toml
[project]
name = "noeta-plugin-house-style"
version = "0.1.0"
dependencies = ["noeta-sdk"]

[project.entry-points."noeta.plugins"]
house-style = "house_style"

[tool.noeta]
name = "house-style"
requires-noeta = ">=0.6"

[[tool.noeta.contributions]]
surface = "prompt_fragment"
name    = "house-style"
text    = "Answer in at most three sentences."

[[tool.noeta.contributions]]
surface = "tool"
ref     = "house_style.tools:LintTool"
```

`python -m noeta.sdk.plugin_check PATH` 检查 `PluginBuilder` 文件和随包发布的清单是否一致；加 `--emit` 会打印按 builder 推出来的清单。

host 加载已安装的插件时带一个白名单，名单外的插件在 import 之前就被跳过：

```python
pset = load_plugins(entry_points=True, enabled=["house-style"])
```

开发环境也可以从目录加载插件（`user_dirs=`，或者先 `grant_trust(path)` 再用 `workspace_dirs=`）。目录里的插件就是在你进程里跑的普通 Python 代码，只信任你本来就愿意运行的代码。`examples/plugins/` 下有六个完整的插件可以参考。

## 下一步

- [插件清单](../reference/plugin-manifest.md)：清单字段、加载来源、信任、版本约束
- [插件扩展点](../reference/plugin-surfaces.md)：每个扩展点及对应的内置示例
- [插件系统](../how-it-works/plugin-system.md)：加载和启用是怎么配合的
