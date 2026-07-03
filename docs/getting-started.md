# Quickstart

Two paths: a no-API-key 90-second smoke that proves install + wiring,
and a real-provider walkthrough that exercises Noeta's headline
capability (subtask suspend/resume).

## Prerequisites

* Python 3.11+
* `pip` or `uv`
* A local checkout of this repository (Noeta does not publish to PyPI
  in Phase 2)

## Install

```bash
# Local checkout (recommended for evaluation)
uv pip install -e apps/noeta-agent

# Direct from git (noeta-agent app-shell subdirectory)
pip install "noeta-agent @ git+<https://github.com/your/repo.git>#subdirectory=apps/noeta-agent"
```

After install, `python -m noeta.agent` is the entry point: it boots the
official coding agent (the offline stub provider by default) and serves
the bundled web app. There is no `noeta` console script — the runtime is
configured from `NOETA_AGENT_*` env vars, not positional CLI args.

> Note: PyPI names `noeta`, `noeta-sdk`, and `noeta-agent` are taken by
> unrelated projects. Install from local checkout or git URL until the
> project picks release names.

## Path 1 — 90-second no-key smoke

The `stub` provider returns a deterministic two-turn LLM double —
no API key, no network call. Boot the offline runner:

```bash
python -m noeta.agent   # serves at an OS-assigned port; Ctrl-C to stop
```

Or drive the backend in-process — build it, prove it serves, shut it down:

<!-- runnable: smoke -->
```python
from noeta.agent.backend.lifecycle import BackendConfig, serve_backend

# Defaults are fully offline: stub provider, in-memory storage. port=0 binds
# an OS-assigned port.
config = BackendConfig(port=0)
server, url, shutdown = serve_backend(config)
try:
    assert url.startswith("http://")
finally:
    shutdown()
```

The backend binds an ephemeral port and serves the bundled web app in
under a second.

The recording lives entirely in memory; nothing is persisted.

## Path 2 — real-provider demo (with API key)

This path exercises subtask suspend/resume against a real
OpenAI-compatible or Anthropic endpoint.

```bash
# OpenAI-compatible
NOETA_OPENAI_BASE_URL=https://api.openai.com/v1 \
NOETA_OPENAI_API_KEY=sk-… \
NOETA_OPENAI_MODEL=gpt-4o-mini \
python examples/_internal/real_provider_subtask_demo.py
```

```bash
# Anthropic (the demo supplies max_tokens=1024 by default; override
# with NOETA_MAX_TOKENS=… if you need a different cap)
NOETA_PROVIDER=anthropic \
NOETA_API_KEY=sk-ant-… \
NOETA_MODEL=claude-3-5-sonnet-20240620 \
python examples/_internal/real_provider_subtask_demo.py
```

The demo:

1. Spawns a parent task (scripted policy) that immediately spawns a
   child task (ReAct policy with the real provider).
2. The parent suspends waiting for the child; the child runs the
   real LLM, calls the `echo` tool, and finishes.
3. The parent is woken via the wake-resume path (`Lease.wake_event`
   carries the child's `SubtaskResult`), runs its second scripted
   decision, and terminates.

A missing required env var prints `skipped: …` and exits 0, so the
script is safe to run in CI even when credentials are unavailable.

## With the Web UI

There is no separate UI flag — `python -m noeta.agent` always serves the
bundled web app. Boot the offline runner and open the chat composer in a
browser:

```bash
NOETA_AGENT_PROVIDER=stub python -m noeta.agent
```

The runner binds `NOETA_AGENT_HOST` (default `127.0.0.1`) and
`NOETA_AGENT_PORT` (`0` ⇒ an OS-assigned ephemeral port), then serves the
web app; visit `<url>/chat` to compose a goal and open
`<url>/trace?task={id}` from a session to inspect its event stream and context.
The pages subscribe to the SSE endpoint `GET /stream?task=<id>`. The server blocks until `Ctrl-C` (SIGINT/SIGTERM)
— there is no grace-window flag. (The old `noeta run --serve --ui` console flags
were removed in TL6.)

## Coding agent

`python -m noeta.agent` *is* the workspace-scoped coding agent: it reads,
edits, runs tests, and records every step. Point it at a directory via
env and start the server (the `noeta code --workspace …` CLI form was
removed in TL6 — the workspace is now `NOETA_AGENT_WORKSPACE`):

```bash
NOETA_AGENT_WORKSPACE=./my-project \
NOETA_AGENT_PROVIDER=openai \
NOETA_AGENT_MODEL=gpt-4o-mini \
NOETA_AGENT_BASE_URL=https://api.openai.com/v1 \
NOETA_AGENT_API_KEY=sk-… \
NOETA_AGENT_SQLITE=./session.sqlite \
python -m noeta.agent
```

Then drive the agent through the web chat composer at `<url>/chat`, or
over HTTP: `POST /tasks` (body: `goal` + `agent` + an optional model
*selector* — provider/credentials are never read from the body, the
host config is authoritative). The `agent` field picks a named agent
(e.g. the `main` general-purpose profile); skills are part of that agent wiring rather
than a per-call flag. Shell still goes through a narrow argv-only
allowlist (pytest / git status&diff / npm-pnpm test).

Once a session is recorded (set `NOETA_AGENT_SQLITE` to a file rather
than the in-memory default), you can manage it without re-running the
agent over the HTTP surface (the `noeta code list/inspect/tail/resume/…`
sub-actions were removed in TL6):

* `GET /tasks` — session list; `GET /stream?task=<id>` — live SSE event
  stream (the read-only views)
* `POST /tasks` — create a task (`goal` + `agent`); `POST /tasks/{id}/messages`
  — append a follow-up goal to an existing task
* `POST /tasks/{id}/approve` / `POST /tasks/{id}/deny` — approve or deny a
  gated tool call; `POST /tasks/{id}/answer` — answer a model-asked question
* `POST /tasks/{id}/close` / `POST /tasks/{id}/reopen` / `POST /tasks/{id}/cancel`
  — close, reopen, or cancel (lifecycle); `DELETE /tasks/{id}` — hard-delete

For read-only inspection without the server you can also call
`noeta.core.fold.fold(event_log, content_store, task_id)` in-process. See
[`docs/noeta-agent.md`](noeta-agent.md) for the full reference (tools,
presets, skills, write/shell policy, HTTP surface).

## What's next

* [Concepts](concepts.md) — the model behind Task /
  EventLog / Engine
* [Noeta Agent](noeta-agent.md) — the `python -m noeta.agent`
  workspace-scoped coding agent (tools, presets, skills, HTTP surface)
* [Failure Modes](failure-modes.md) — common failures and
  how to recover
* [Daemon / Worker Loop](daemon.md) — the resident drain loop, now the
  library primitive `noeta.runtime.worker.WorkerLoop` (the `noeta serve`
  daemon was removed in TL6)
