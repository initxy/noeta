# Use the coding agent

**Goal:** drive `python -m noeta.agent` for real coding tasks — configure
the workspace, use agent presets, manage sessions, and work with skills.

**Before you start:** you have installed Noeta and configured a real
provider (see [Configure a provider](configure-provider.md)).

## Start the agent with your workspace

```bash
NOETA_AGENT_WORKSPACE=./my-project \
NOETA_AGENT_PROVIDER=anthropic \
NOETA_AGENT_MODEL=claude-sonnet-4-5-20250929 \
NOETA_AGENT_API_KEY=sk-ant-… \
NOETA_AGENT_STORAGE=./my-project/session.sqlite \
NOETA_AGENT_WRITE_MODE=apply \
python -m noeta.agent
```

Key variables:

| Variable | Why set it |
| --- | --- |
| `NOETA_AGENT_WORKSPACE` | The directory the agent reads and edits. Defaults to `.`. |
| `NOETA_AGENT_STORAGE` | Durable storage for the EventLog. Without this, sessions die with the process. |
| `NOETA_AGENT_WRITE_MODE` | `apply` lets the agent actually write files. Default `dry_run` proposes diffs only. |

Open the printed URL in your browser and navigate to `/chat`.

## Choose an agent preset

The agent is chosen **per task** — when you create a new conversation,
you can pick which agent to use. The four shipped presets:

| Preset | When to use |
| --- | --- |
| `main` | Default. Full tool surface, spawns sub-agents. Best for general coding work. |
| `general-purpose` | Self-contained: reads, writes, edits, runs shell. No delegation. |
| `explore` | Read-only scout. Use it to understand a codebase without risk of edits. |
| `plan` | Read-only architect. Returns an ordered implementation plan. |

Pick `explore` when you want the agent to understand a new codebase
without touching anything; pick `main` when you want it to make changes.

## Send a message and watch the trace

Type your request in the chat composer — for example:

```
Find all Python files that import `pydantic` and list what they use from it.
```

As the agent works, the trace view fills with events: the LLM turn, each
tool call (`grep`, `read`, `glob`), and the tool results. You can
inspect token usage and cache hit rates per turn.

If the agent proposes an edit and `NOETA_AGENT_WRITE_MODE=apply`, the
file changes immediately. If `write_mode=dry_run` (the default), you see
a unified diff artifact instead — safe for evaluation.

## Manage sessions

The left sidebar shows your session list (root conversations only;
subtasks ride on their parent's stream). Each row shows the status,
title (from the first message), and agent name.

- **Create** — click "New session" or send a message from an empty state.
- **Resume** — click a session to continue it. The agent folds the
  EventLog to recover state, so the conversation picks up where it left
  off even if you restarted the server.
- **Close / reopen** — right-click or use the session menu. Closing
  archives a session; reopening makes it active again.
- **Cancel** — stops a running session mid-turn. The partial state is
  preserved in the log.
- **Delete** — hard-deletes the session and its subtask tree from
  storage. Irreversible.

## Use skills

Skills are Markdown-based capability packs the model can activate on
demand. Drop a skill into your workspace:

```
my-project/
└── .noeta/
    └── skills/
        └── pdf-extract/
            └── SKILL.md
```

`SKILL.md` has YAML frontmatter plus a Markdown body:

```markdown
---
name: pdf-extract
description: Extract text and tables from PDF files
version: "1"
---

# PDF Extract

Use `pdftotext` (shell) to extract text from a PDF file.
Call it with the file path.
```

When the model decides it needs PDF extraction, it calls `skill:
pdf-extract`, and the skill body is folded into the next turn's context.
The model then uses the bundled resources (via `read`) to carry out the
task.

Global skills go in `~/.noeta/skills/` and are available to all
workspaces.

## Approve gated tool calls

When `NOETA_AGENT_WRITE_MODE=apply` and `permission_mode=default` (the
default for `main`), certain tool calls require your approval before
they execute:

- `edit`, `write`, `apply_patch` — file modifications
- `shell_run` — shell commands (even with `allowlist` mode, some
  commands may need approval)

The chat UI shows a pending approval with the tool call details. Click
**Approve** to let it run, or **Deny** to block it. The approval or
denial is recorded in the EventLog.

## See also

- [Coding agent reference](../reference/noeta-agent.md) — every env var,
  tool, and preset
- [HTTP API reference](../reference/http-api.md) — the routes behind the UI
- [Configure a provider](configure-provider.md) — connect a real LLM
- [Build custom tools](build-custom-tools.md) — extend the tool surface
