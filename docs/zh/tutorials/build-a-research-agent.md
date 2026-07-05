# 教程：构建研究代理 { #tutorial-build-a-research-agent }

端到端：安装 Noeta、配置 provider、构建一个可以搜索网络并撰写报告的研究代理，然后检查它做了什么。

## 前置条件 { #prerequisites }

- Python 3.11+
- OpenAI 兼容 provider 的 API key（或使用离线 stub 跟随操作而无需 key）

## 步骤 1：安装 { #step-1-install }

```bash
pip install noeta-agent
```

这会装上 SDK 和 runtime，web 前端已预构建进 wheel。

## 步骤 2：配置 provider { #step-2-configure-your-provider }

创建一个配置文件 `noeta.config.json`：

```json
{
  "provider_id": "openai",
  "model": "gpt-5.5",
  "base_url": "https://api.openai.com/v1",
  "api_key": "<your-api-key>",
  "workspace_dir": ".",
  "storage_url": ":memory:",
  "host": "127.0.0.1",
  "port": 8765
}
```

> **没有 API key？** 设置 `"provider_id": "stub"` 并省略 `api_key` / `base_url`。离线 stub provider 以脚本化响应回答——足以看到 UI 和事件流，但不会做真正的研究。

对于 Web 搜索，设置环境变量（可选——没有它代理也能工作）：

```bash
export NOETA_WEB_SEARCH_API_KEY=<your-tavily-or-similar-key>
```

## 步骤 3：启动代理 { #step-3-launch-the-agent }

```bash
make run
```

你应该看到：

```
▶ noeta.agent → http://127.0.0.1:8765/chat
```

在浏览器中打开该 URL。

## 步骤 4：选择合适的代理预设 { #step-4-pick-the-right-agent-preset }

在聊天 UI 中，从下拉菜单中选择 **`main`** 代理。`main` 预设拥有完整的工具集：

| 工具 | 用途 | 风险 |
| --- | --- | --- |
| `read` | 读取工作区文件 | low |
| `glob` | 匹配 glob 模式 | low |
| `grep` | 正则内容搜索 | low |
| `webfetch` | 获取网页为 Markdown | low |
| `web_search` | Web 搜索（key 门控） | low |
| `write` | 写入文件 | high |
| `edit` | 替换文件中的文本 | high |
| `apply_patch` | 原子性批量编辑 | high |
| `shell_run` | 运行 shell 命令 | high |

`main` 代理还有 `delegation` 能力（可以生成子代理）和 `memory`（跨任务召回）。

## 步骤 5：给它一个研究任务 { #step-5-give-it-a-research-task }

在聊天中输入：

```
Research the latest advances in retrieval-augmented generation (RAG)
from 2025-2026. Search the web for at least 3 sources, read them,
and write a structured summary to reports/rag-2025.md.
Include citations for every claim.
```

接下来发生的事情：

1. 模型使用类似 `"RAG advances 2025 2026"` 的查询调用 `web_search`。
2. 它以 Markdown 形式获得排名结果。
3. 它在前 3 个 URL 上调用 `webfetch` 以获取完整内容。
4. 它阅读并交叉引用内容。
5. 它调用 `write` 创建 `reports/rag-2025.md`。

> **写入安全：** 默认情况下，`write` 是 **dry-run**。代理暂存一个 unified diff 但不实际修改字节。要启用真实写入，设置 `NOETA_AGENT_WRITE_MODE=apply`（或配置中的 `"write_mode": "apply"`）。见[配置](../reference/configuration.md)。

## 步骤 6：查看 trace { #step-6-watch-the-trace }

点击 UI 中的 **Trace** 选项卡。你会看到每一步：

- `LLMRequestStarted` / `LLMRequestCompleted` —— 模型调用
- `ToolCallStarted` / `ToolResultRecorded` —— 工具调用
- `MessagesAppended` —— 上下文更新
- `TaskCompleted` —— 最终答案

每个 envelope 显示 `seq`、`type`、`actor` 和 `trace_id`。这就是 EventLog——唯一的真相来源。

## 步骤 7：以编程方式检查 EventLog { #step-7-inspect-the-eventlog-programmatically }

想更深入地挖掘？使用 SDK fold 和检查事件流：

```python
from noeta.sdk import Client, Options
from pathlib import Path

# Connect to a running backend via the SDK (in-process mode)
options = Options(
    system_prompt="You are a researcher.",
    name="main",
    allowed_tools=("read", "webfetch", "web_search", "write"),
    permission_mode="bypassPermissions",
)

# ... run a query, then:
# events = client.events(task_id)
# for env in events:
#     print(f"seq={env.seq} type={env.type}")
#     if env.type == "ToolCallStarted":
#         print(f"  tool={env.payload.tool_name} args={env.payload.arguments}")
```

## 步骤 8：自定义代理 { #step-8-customize-the-agent }

想要一个从不编辑代码的更精简的研究代理？通过 `Options.agents` 创建一个自定义代理：

```python
from noeta.sdk import Options, AgentDefinition

research_agent = Options(
    system_prompt="""You are a research agent.
- Search the web for sources.
- Fetch and read at least 3.
- Write a cited summary.
- Never edit existing code files.""",
    name="researcher",
    allowed_tools=("read", "glob", "grep", "webfetch", "web_search", "write"),
    permission_mode="default",
    agents={
        "fact-checker": AgentDefinition(
            description="Verifies claims against sources.",
            prompt="You fact-check claims by reading sources.",
            tools=["read", "webfetch"],
        ),
    },
)
```

或以编程方式使用官方预设：

```python
from noeta import presets
options = presets.main_options()  # full main agent
```

## 你学到了什么 { #what-you-learned }

- 如何使用真实 provider 安装和启动 Noeta
- `main` 预设开启了哪些工具及其风险等级
- EventLog 如何记录每一步
- 如何使用 `Options` 自定义代理配方
- 在哪里找 trace 视图以及如何阅读它

## 接下来 { #next-steps }

- [权限门控](../guides/permission-gating.md) —— 控制哪些工具调用实际执行
- [子代理委派](../guides/subagent-delegation.md) —— 生成专门的子代理
- [持久化存储](../guides/durable-storage.md) —— 在重启后持久化会话
- [MCP 连接器](../guides/mcp-connectors.md) —— 连接外部 MCP 工具服务器
