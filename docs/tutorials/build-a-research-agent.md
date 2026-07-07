# Tutorial: Build a research agent

End-to-end: install Noeta, configure a provider, build a research agent
that can search the web and write reports, then inspect what it did.

## Prerequisites

- Python 3.11+
- An API key for an OpenAI-compatible provider (or use the offline stub
  to follow along without one)

## Step 1: Install

```bash
pip install noeta-agent
```

This pulls in the SDK and runtime, with the web frontend already built into the wheel.

## Step 2: Configure your provider

Create a config file `noeta.config.json`:

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

> **No API key?** Set `"provider_id": "stub"` and omit `api_key` /
> `base_url`. The offline stub provider answers with scripted responses
> — enough to see the UI and event flow, but it won't do real research.

For web search, set the env var (optional — the agent works without it):

```bash
export NOETA_WEB_SEARCH_API_KEY=<your-tavily-or-similar-key>
```

## Step 3: Launch the agent

```bash
make run
```

You should see:

```
▶ noeta.agent → http://127.0.0.1:8765/chat
```

Open that URL in your browser.

## Step 4: Pick the right agent preset

In the chat UI, select the **`main`** agent from the dropdown. The
`main` preset has the full tool surface:

| Tool | What it does | Risk |
| --- | --- | --- |
| `read` | Read a workspace file | low |
| `glob` | Match glob patterns | low |
| `grep` | Regex content search | low |
| `webfetch` | Fetch a web page to Markdown | low |
| `web_search` | Web search (key-gated) | low |
| `write` | Write a file | high |
| `edit` | Replace text in a file | high |
| `apply_patch` | Atomic batch of edits | high |
| `shell_run` | Run a shell command | high |

The `main` agent also has `delegation` capability (can spawn sub-agents)
and `memory` (cross-task recall).

## Step 5: Give it a research task

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
5. It calls `write` to create `reports/rag-2025.md`.

> **Write safety:** By default, `write` is **dry-run**. The agent
> stages a unified diff but doesn't actually change bytes. To enable
> real writes, set `NOETA_AGENT_WRITE_MODE=apply` (or
> `"write_mode": "apply"` in your config). See
> [Configuration](../reference/configuration.md).

## Step 6: Watch the trace

Click the **Trace** tab in the UI. You'll see each step:

- `LLMRequestStarted` / `LLMRequestCompleted` — model calls
- `ToolCallStarted` / `ToolResultRecorded` — tool invocations
- `MessagesAppended` — context updates
- `TaskCompleted` — final answer

Each envelope shows `seq`, `type`, `actor`, and `trace_id`. This is the
EventLog — the single source of truth.

## Step 7: Inspect the EventLog programmatically

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

## Step 8: Customize the agent

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

- How to install and launch Noeta with a real provider
- Which tools the `main` preset opens and their risk levels
- How the EventLog records every step
- How to customize the agent recipe with `Options`
- Where to look for the trace view and how to read it

## Next steps

- [Guard vs Observer](../concepts/guard-observer.md) — control which
  tool calls actually execute
- [Spawn sub-agents](../how-to/spawn-subagents.md) — spawn
  specialized child agents
- [Deploy a worker](../how-to/deploy-worker.md) — persist sessions
  across restarts on a durable store
- [Connect MCP](../how-to/connect-mcp.md) — connect external
  MCP tool servers
