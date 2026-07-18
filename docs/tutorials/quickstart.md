# Quickstart: see Noeta running in 5 minutes

**What you'll do:** boot the platform with zero credentials, log in, hold a
scripted conversation, and see it replay from the event log. **No API key,
no Docker, no accounts** — the default mock provider is a deterministic LLM
double.

## 1. Install

You need Python 3.11+ with [uv](https://docs.astral.sh/uv/) and Node 20+:

```bash
git clone https://github.com/initxy/noeta && cd noeta
make install        # uv sync + frontend deps
```

## 2. Boot the platform

```bash
make run            # build the SPA + python -m noeta.agent
```

This starts the server on <http://127.0.0.1:8000> — offline mock LLM,
dev-login, SQLite storage, sandbox off (the zero-credential mode; every
config key is optional). The underlying entry point is always
`python -m noeta.agent`, env-only, no flags. Ctrl-C stops it.

## 3. Log in and talk

Open the URL and log in with **any username** (dev-login — the development
auth provider). You land in your personal space. Start a session and send a
message — for example:

```text
Write me a short report on the state of the project.
```

The mock provider plays a scripted demo through the *real* machinery: the
agent asks you a clarifying question (answer it), activates a skill, and
writes back its answer. Every one of those moments — the question, the
skill activation, each turn boundary — is a recorded event.

## 4. See the log underneath

Reload the page mid-conversation: the stream rebuilds exactly, because the
UI replays by **re-deriving from the event log** rather than trusting
anything held in memory. To see the raw record, add your username to
`ADMIN_USERS` in `apps/noeta-agent/.env`, restart, and open the admin
console's **Trace** view — the untranslated engine events (LLM turns, tool
calls, token/cache stats) for any session. See
[Event sourcing](../concepts/event-sourcing.md) for why this matters.

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

- **Connect a real model** — [Configure a provider](../how-to/configure-provider.md):
  any OpenAI-Responses-compatible gateway, in two `.env` lines.
- **Turn on the sandbox** — `SANDBOX_ENABLED=true` + Docker gives every
  session its own container with live Browser / Terminal / Code panels; see
  [Use the platform](../how-to/use-the-coding-agent.md).
- **Build your own agent** — [Your first agent](first-agent.md) is a
  20-minute guided SDK walkthrough using `@tool`, `Options`, and `query()`.
- **Understand the design** — start with [Event sourcing](../concepts/event-sourcing.md)
  and the [architecture overview](../architecture/overview.md).
