# 快速上手

五分钟，让一个真正的 agent 在你的文件上干活。

## 1. 安装

```bash
uv pip install noeta-sdk        # 或者：pip install noeta-sdk
```

需要 Python 3.11 或更高版本。要导入的东西都在 `noeta.sdk` 里。`Glob` 和 `Grep` 工具要用 [ripgrep](https://github.com/BurntSushi/ripgrep)：`rg` 必须在 `PATH` 上（`apt install ripgrep`、`brew install ripgrep`）。

## 2. 设置 API key

```bash
export ANTHROPIC_API_KEY=sk-ant-...
```

用 OpenAI 或别的网关？看[第 5 步](#_5-换一个模型)。

## 3. 跑一个 agent

在任意项目目录下存成 `agent.py`，然后执行 `python agent.py`：

```python
from noeta.sdk import Options, query
from noeta.sdk.providers import AnthropicProvider

result = query(
    Options(system_prompt="You are a concise coding assistant."),
    goal="What files are in this directory, and what does each one do?",
    provider=AnthropicProvider(),
    model="claude-sonnet-5",
)
print(result.answer())
```

agent 会列出目录、读它需要的文件，然后回答。每次措辞不一样，对一个只有两个文件的项目，
大致是这样：

```
The directory contains two files:

- app.py — a one-line Python script that prints "hi".
- README.md — a README with just the heading "demo".
```

刚才用到的几样东西：

| 名字 | 是什么 |
|---|---|
| `Options` | agent 本身：系统提示词、工具、权限。不指定 `allowed_tools` 时带全套内置工具（读、搜、改文件，跑 shell 命令，抓网页）。 |
| `query` | 把一个任务从头跑到尾，返回结果。 |
| `AnthropicProvider()` | 模型连接，读 `ANTHROPIC_API_KEY`。 |
| `model` | 你的接口支持的任意模型 id。 |

agent 在当前目录里干活。想换目录，给 `query` 传 `workspace_dir="path"`。

## 4. 看看发生了什么

`result` 同时也是这次运行的完整记录：每次模型调用、工具调用和 token 用量，按顺序排好：

```python
for event in result:
    print(event.seq, event.type)

for message in result.messages():   # 可读的对话形式
    print(message)
```

```
0 TaskCreated
1 AgentBound
...
7 LLMRequestStarted
...
35 TaskCompleted
```

Noeta 存下来、重放的就是这份日志。任务靠它扛过崩溃，你事后也靠它审计。

## 5. 换一个模型

任何兼容 OpenAI `/chat/completions` 的接口（读 `OPENAI_API_KEY`）：

```python
from noeta.sdk.providers import OpenAICompatProvider

provider = OpenAICompatProvider(base_url="https://api.openai.com/v1")
result = query(options, goal="...", provider=provider, model="gpt-4o")
```

agent 本身不用改。OpenAI Responses API、自建网关、目录里没有的模型等其他情况，见
[接入模型](../guides/models.md)。

::: tip 有风险的调用会先问你
默认情况下，改文件（`Edit`、`Write`）和不在内置安全名单里的 shell 命令（`ls`、`git status`
这类在名单里）会暂停任务，等你批准后才执行。所以 `query` 适合只读的活；怎么用 `Client`
批准调用，见[教程](tutorial.md)。想跳过审批就设 `permission_mode="bypassPermissions"`，
只在确定安全的地方这么做。
:::

## 接下来

- [教程：搭一个完整的 agent](tutorial.md)：自己写工具、审批、多轮对话、重启不丢的存储
- [为什么选 Noeta](../why-noeta.md)：它和别的方案有什么不同
- [离线测试与 CI](../guides/testing.md)：不用 API key 也能跑 agent
