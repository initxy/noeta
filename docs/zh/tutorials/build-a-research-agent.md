# 教程：构建研究代理 { #tutorial-build-a-research-agent }

端到端：让平台对接一个真实网关，给代理一个搜索网络并撰写报告的研究任务，然后检查它做了什么。

## 前置条件 { #prerequisites }

- Python 3.11+ 及 [uv](https://docs.astral.sh/uv/)，Node 20+
- OpenAI-Responses 兼容网关的 API key（或使用离线 mock 走一遍流程而无需 key）
- Docker，用于沙箱（代理需要它来写报告文件）

## 步骤 1：安装 { #step-1-install }

```bash
git clone https://github.com/initxy/noeta && cd noeta
make install
```

## 步骤 2：配置网关与沙箱 { #step-2-configure-the-gateway-and-sandbox }

编辑 `apps/noeta-agent/.env`（复制 `.env.example`）：

```dotenv
LLM_BASE_URL=https://your-gateway.example.com/v1
LLM_API_KEY=<your-api-key>
SANDBOX_ENABLED=true
```

并把你网关的模型 id 写进 `apps/noeta-agent/models.json`（见[配置 provider](../how-to/configure-provider.md)）。

> **没有 API key？** 全部留空即可。离线 mock provider 会播放一段脚本化对话——足以看到 UI 和事件流，但不会做真正的研究。

对于 Web 搜索，设置环境变量（可选——没有它代理也能工作，只用 `webfetch`）：

```bash
export NOETA_WEB_SEARCH_API_KEY=<your-tavily-or-similar-key>
```

## 步骤 3：启动并登录 { #step-3-launch-and-log-in }

```bash
make run
```

打开 <http://127.0.0.1:8000>，用任意用户名登录（dev-login），在你的个人 space 中开始一个新会话。如果你的 `models.json` 提供了多个选择，在 composer 中选择模型和推理力度（reasoning effort）。

## 步骤 4：给它一个研究任务 { #step-4-give-it-a-research-task }

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
5. 它调用 `write` 创建 `reports/rag-2025.md`——**在会话的沙箱容器内**；文件落在会话工作区中，右侧的 **Files** 面板会显示它。

执行是 sandbox-only 的：每个文件和 shell 副作用都发生在每会话一个的容器内，从不落在你的主机上。没有沙箱时，代理仍然可以在聊天中搜索和总结，但没有文件表面。

## 步骤 5：查看 trace { #step-5-watch-the-trace }

把你的用户名加进 `.env` 的 `ADMIN_USERS`（需重启），然后在管理控制台中打开你会话的 **Trace** 视图。你会看到每一步：

- `ContextPlanComposed` —— 模型看到了什么
- `ToolCallStarted` / `ToolResultRecorded` —— 工具调用
- `MessagesAppended` —— 上下文更新
- `TaskCompleted` —— 最终答案

每个 envelope 显示 `seq`、`type`、`actor` 和 `trace_id`。这就是 EventLog——唯一的真相来源；你刚才使用的聊天视图正是从这条流派生出来的翻译。

## 步骤 6：以编程方式检查 EventLog { #step-6-inspect-the-eventlog-programmatically }

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

## 步骤 7：自定义代理 { #step-7-customize-the-agent }

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

- 如何把平台指向一个真实网关并开启沙箱
- 一次研究轮次如何分解为 search / fetch / write 工具调用
- EventLog 如何记录每一步
- 如何使用 `Options` 自定义代理配方（SDK）
- 在哪里找到管理端 trace 视图以及如何阅读它

## 接下来 { #next-steps }

- [Guard 与 Observer](/concepts/guard-observer) —— 控制哪些工具调用实际执行
- [生成子代理](/how-to/spawn-subagents) —— 生成专门的子代理
- [部署 worker](/how-to/deploy-worker) —— 在持久化存储上跨重启持久化会话
- [连接 MCP](/how-to/connect-mcp) —— 连接外部 MCP 工具服务器
