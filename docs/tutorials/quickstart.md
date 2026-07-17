# Quickstart: see Noeta running in 5 minutes

**What you'll do:** install Noeta, boot the offline coding agent, open the
web UI, send a message, and inspect the trace. **No API key needed** — the
default `stub` provider is a deterministic LLM double.

## 1. Install

```bash
pip install noeta-agent
```

This pulls in the SDK and runtime. You need Python 3.11+.

## 2. Boot the agent

```bash
python -m noeta.agent
```

This starts the coding agent server with the offline `stub` provider and
in-memory storage. It prints a URL — something like
`http://127.0.0.1:54321/`. The server blocks until Ctrl-C; there is no
daemon mode.

## 3. Open the web UI

Point your browser at the printed URL, then navigate to `/chat`. You
should see the chat composer. Type a message — for example:

```
List the Python files in this directory and tell me what each one does.
```

The `stub` provider returns a scripted two-turn response, so you will see
the agent "think" and then reply with a canned answer. The point is not
the quality of the response — it is that every step is recorded.

## 4. View the trace

From the chat view, click on a session to open its trace. The trace view
shows every event in the session's EventLog: the user message, the LLM
turn, any tool calls, the tool results, and the final answer. Each row
includes token counts and cache statistics.

This trace is not generated from process memory — it is folded from the
EventLog, the same way the agent's own state is recovered. See
[Event sourcing](../concepts/event-sourcing.md) to understand why this
matters.

## 5. Drive it in-process (optional)

If you prefer code over a browser, the same application assembles in a few
lines (serving it is one `uvicorn.run` away):

<!-- runnable: smoke -->
```python
from noeta.agent.main import create_app

# Fully offline defaults: the deterministic mock LLM, SQLite app storage,
# dev-login. create_app assembles the FastAPI application without serving it.
app = create_app()
assert "/api/v1/health" in app.openapi()["paths"]
print("application assembled")
```

## Next steps

- **Connect a real model** — [Configure a provider](../how-to/configure-provider.md)
  walks through Anthropic and OpenAI-compatible setups.
- **Build your own agent** — [Your first agent](first-agent.md) is a
  20-minute guided SDK walkthrough using `@tool`, `Options`, and `query()`.
- **Understand the design** — start with [Event sourcing](../concepts/event-sourcing.md)
  and the [architecture overview](../architecture/overview.md).
- **Use the coding agent for real work** — [Use the coding agent](../how-to/use-the-coding-agent.md)
  covers workspace setup, presets, and skills.
