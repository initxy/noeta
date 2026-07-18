# Tutorial: Build a research agent

End-to-end: boot the platform against a real gateway, give the agent a
research task that searches the web and writes a report, then inspect what
it did.

## Prerequisites

- Python 3.11+ with [uv](https://docs.astral.sh/uv/), Node 20+
- An API key for an OpenAI-Responses-compatible gateway (or use the
  offline mock to follow the motions without one)
- Docker, for the sandbox (the agent needs it to write report files)

## Step 1: Install

```bash
git clone https://github.com/initxy/noeta && cd noeta
make install
```

## Step 2: Configure the gateway and sandbox

Edit `apps/noeta-agent/.env` (copy `.env.example`):

```dotenv
LLM_BASE_URL=https://your-gateway.example.com/v1
LLM_API_KEY=<your-api-key>
SANDBOX_ENABLED=true
```

and put your gateway's model id into `apps/noeta-agent/models.json` (see
[configure a provider](../how-to/configure-provider.md)).

> **No API key?** Leave everything empty. The offline mock provider plays
> a scripted conversation — enough to see the UI and event flow, but it
> won't do real research.

For web search, set the env var (optional — the agent works without it,
using `webfetch` only):

```bash
export NOETA_WEB_SEARCH_API_KEY=<your-tavily-or-similar-key>
```

## Step 3: Launch and log in

```bash
make run
```

Open <http://127.0.0.1:8000>, log in with any username (dev-login), and
start a new session in your personal space. Pick the model and reasoning
effort in the composer if your `models.json` offers choices.

## Step 4: Give it a research task

Type into the chat:

```
Research the latest advances in retrieval-augmented generation (RAG)
from 2025-2026. Search the web for at least 3 sources, read them,
and write a structured summary to reports/rag-2025.md.
Include citations for every claim.
```

What happens next:

1. The model calls `web_search` with a query like `"RAG advances 2025 2026"`.
2. It gets ranked hits back as Markdown.
3. It calls `webfetch` on the top 3 URLs to get full content.
4. It reads and cross-references the content.
5. It calls `write` to create `reports/rag-2025.md` — **inside the
   session's sandbox container**; the file lands in the session workspace,
   and the **Files** panel on the right shows it.

Execution is sandbox-only: every file and shell side effect happens in the
per-session container, never on your host. Without the sandbox the agent
can still search and summarize in chat, but has no file surface.

## Step 5: Watch the trace

Add your username to `ADMIN_USERS` in `.env` (restart) and open the admin
console's **Trace** view for your session. You'll see each step:

- `ContextPlanComposed` — what the model was shown
- `ToolCallStarted` / `ToolResultRecorded` — tool invocations
- `MessagesAppended` — context updates
- `TaskCompleted` — final answer

Each envelope shows `seq`, `type`, `actor`, and `trace_id`. This is the
EventLog — the single source of truth; the chat view you just used is a
translation derived from exactly this stream.

## Step 6: Inspect the EventLog programmatically

Want to dig deeper? Use the SDK to fold and inspect the event stream:

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

## Step 7: Customize the agent

Want a leaner research agent that never edits code? Create a custom
agent via `Options.agents`:

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

Or use the official presets programmatically:

```python
from noeta import presets
options = presets.main_options()  # full main agent
```

## What you learned

- How to point the platform at a real gateway and turn on the sandbox
- How a research turn decomposes into search / fetch / write tool calls
- How the EventLog records every step
- How to customize an agent recipe with `Options` (SDK)
- Where to find the admin trace view and how to read it

## Next steps

- [Guard vs Observer](../concepts/guard-observer.md) — control which
  tool calls actually execute
- [Spawn sub-agents](../how-to/spawn-subagents.md) — spawn
  specialized child agents
- [Deploy a worker](../how-to/deploy-worker.md) — persist sessions
  across restarts on a durable store
- [Connect MCP](../how-to/connect-mcp.md) — connect external
  MCP tool servers
