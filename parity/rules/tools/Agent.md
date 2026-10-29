# Agent

Claude Code 2.1.312 · baseline `m1` · rules in our words (see `../README.md`).

| source | sha256 |
|---|---|
| `m1/0001 tools[Agent].description` | `e3567c6aef52da31f6c0ff5f2118d0773ec6c12576bc381025207377216ac88d` |
| `m1/0001 tools[Agent].input_schema` | `c2e60af139fce569d6987da13ecce6f5b2b469822633a692acb08533a51e61e3` |

## Purpose

Starts a sub-agent (fresh, or a fork of the caller) for multi-step work. Always loaded (no
`defer_loading`); position 1 of 13 in the baseline list (tools sorted by name). noeta's
counterpart is `Agent` (`noeta/plugins/subtasks/tool.py`, `descriptions/agent.md`).

## Parameters

`additionalProperties: false`; required: `description`, `prompt`.

| id | name | type | required | enum / default / limits | noeta today |
|---|---|---|---|---|---|
| `tool.Agent.p.description` | `description` | string | yes | a 3–5 word label of the task | covered |
| `tool.Agent.p.prompt` | `prompt` | string | yes | the task text for the agent | covered |
| `tool.Agent.p.subagent_type` | `subagent_type` | string | no | free string, no enum; omitted = `general-purpose`; `"fork"` = fork of the caller | covered |
| `tool.Agent.p.model` | `model` | string | no | enum `sonnet`, `opus`, `haiku`, `fable`; precedence: this field > agent file `model` > configured default sub-agent model > inherit the parent; ignored for `fork` | covered |
| `tool.Agent.p.effort` | `effort` | string | no | enum `low`, `medium`, `high`, `xhigh`, `max`; set only when the user or loaded instructions explicitly ask for a level; ignored for `fork` | covered |
| `tool.Agent.p.isolation` | `isolation` | string | no | enum `worktree`, `remote`; `worktree` = a temporary git worktree; `remote` = cloud run, always background, availability gated | covered (`worktree`; `remote` is n/a: cloud feature, refused) |
| — | `background` | — | — | not in the baseline schema | covered — removed; every call is background |

## Rules

| id | rule | noeta today |
|---|---|---|
| `tool.Agent.1` | The tool launches an agent for complex, multi-step work; each agent type has its own capabilities and tool set. | covered |
| `tool.Agent.2` | The list of agent types is not in the tool definition; it is announced in the conversation (the `role: "system"` message, see `main-prompt.md`). | covered |
| `tool.Agent.3` | `subagent_type: "fork"` copies the caller: the fork inherits the whole conversation and always runs on the caller's model; a `model` value is ignored for it. | covered |
| `tool.Agent.4` | Any other `subagent_type`, or none, starts a fresh agent; none means the general-purpose type. | covered |
| `tool.Agent.5` | Cost framing: a fresh agent knows only its prompt, the caller sees only its summary, and detail is lost in both directions without either side knowing what was missed. | covered |
| `tool.Agent.6` | The caller cannot watch an agent work; it can only wait for it or cancel it. | covered |
| `tool.Agent.7` | An agent's mistakes sound as confident as its findings, and an agent handed the caller's hypothesis tends to confirm it. | covered |
| `tool.Agent.8` | Several agents at once burn tokens the user did not ask for; weigh token cost against the accuracy gained — unneeded agents and work redone because no agent was used both cost the user. | covered |
| `tool.Agent.9` | Use it for: independent work that can run in parallel; a side task the user wants off the main thread; questions that need reading across several files (keep the conclusion, not the dumps). | covered |
| `tool.Agent.10` | Do it yourself when it is a few tool calls or a lookup whose target is known; never delegate a check that can run inline. | covered |
| `tool.Agent.11` | Delegate a review only to get a read not anchored on your own; give the reviewer the code, not your conclusion. | covered |
| `tool.Agent.12` | Once work is delegated, do not also do it yourself; wait for the result. | covered |
| `tool.Agent.13` | When unsure whether to spawn, don't. | covered |
| `tool.Agent.14` | Brief the agent as a peer: the goal, what is already ruled out, pointers to files/docs instead of pasted content, an explicit narrow scope; the brief is its only context, and failing to write a clear brief means the task is not understood yet. | covered |
| `tool.Agent.15` | A fork works in the background, and its tool output never enters the caller's context; a fork itself must do the work, not delegate again. | covered |
| `tool.Agent.16` | Sub-agents run in the background; the caller is notified when one finishes. | covered |
| `tool.Agent.17` | Never invent or predict a pending agent's result; the completion notice is never something the model writes itself; if the user asks before it arrives, say it is still running. | covered (background ack says not to fabricate) |
| `tool.Agent.18` | The user does not see the agent's final report; the caller passes on the parts that matter. | covered |
| `tool.Agent.19` | `SendMessage` with the agent's id or name continues a finished agent with its context; a new `Agent` call starts fresh (except a fork, which inherits). | covered |
| `tool.Agent.20` | Model, effort and tools of an agent type come from its definition (the frontmatter of `.claude/agents/*.md`, or an agent defined through the SDK). | covered — `.noeta/agents/` (`deviations.md`) |
| `tool.Agent.21` | `isolation: "worktree"` runs the agent in its own git worktree, removed automatically when left unchanged. | covered |
| `tool.Agent.22` | Several independent agents go out as several `Agent` calls in one response so they run concurrently (stated in the agent-list message, not here). | covered (in the agent list) |

## Result shape

| id | shape (observed) |
|---|---|
| `tool.Agent.r.1` | The call returns at once (both `Explore` in m1 and `general-purpose` in s1): one `text` block in a list, stating the agent was launched asynchronously and that the result block is internal metadata never to be quoted to the user. |
| `tool.Agent.r.2` | The ack carries `agentId: <id>` (`a` + 16 hex chars), marked internal, plus how to continue it: `SendMessage` with `to: '<id>'` and a 5–10 word `summary`. |
| `tool.Agent.r.3` | The ack says the agent works in the background, a notification will arrive automatically, nothing is known about its result until then (do not report, assume or predict it), and the caller should do other work or answer the user meanwhile. |
| `tool.Agent.r.4` | The ack tells the caller not to duplicate the agent's work (avoid the same files or topics). |
| `tool.Agent.r.5` | The ack ends with `output_file: <tmp>/claude-<uid>/<project-slug>/<session-id>/tasks/<id>.output` and a warning not to read or tail that file through the shell (it is the full JSONL transcript and would flood the context); if asked for progress, say it is still running. |
| `tool.Agent.r.6` | Completion comes later as two user-role pieces: the agent's `SubagentHandback` report wrapped in `<agent-message from="<id>">` (see `SubagentHandback.md`, `subagent.md`), and a `<task-notification>` in a `<system-reminder>` with `task-id`, `tool-use-id`, `output-file`, `status` `completed`, `summary` `Agent "<description>" finished`, a note that the same id may notify again, a `result` that points at the hand-back message instead of repeating it, and `usage` (`subagent_tokens`, `tool_uses`, `duration_ms`). See `reminders.md`. |
| `tool.Agent.r.7` | noeta today: r.1–r.6. The transcript file is `<spill root>/<root task id>/tasks/<agent id>.output` (the host's spill directory for large tool outputs), one JSON line (`type`, `message`) per message of the agent's conversation, rewritten from its log as it changes; a host with no spill directory has no file, and then neither the ack nor the notice names one. |

## Variants

- s1 (OAuth): same description and schema hashes. The ack's tool_result carried
  `cache_control` with `ttl: 1h` (s1 uses the 1-hour cache).
