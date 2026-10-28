# Skill

Claude Code 2.1.312 · baseline `m1` · rules in our words (see `../README.md`).

| source | sha256 |
|---|---|
| `m1/0001 tools[Skill].description` | `a58970596b9b917dc98505b2c8936d0bce66f5082b689dec994cb72868d6ddd3` |
| `m1/0001 tools[Skill].input_schema` | `deda2bb824bf161320465e5993400d314a2f595a45d2e6408dd6bd2e9da838c7` |

## Purpose

Loads a skill (packaged instructions) into the turn, or runs a forked skill. Always loaded;
position 9 of 13 in the baseline list; also present for sub-agents. noeta's counterpart is
`skill` (`noeta/plugins/skills/extension.py`, `descriptions/skill.md`).

## Parameters

`additionalProperties: false`; required: `skill`.

| id | name | type | required | enum / default / limits | noeta today |
|---|---|---|---|---|---|
| — | tool name | — | — | `Skill` | contradicts — noeta names it `skill` |
| `tool.Skill.p.skill` | `skill` | string | yes | a name from the skill listing; never guessed | covered (noeta caps it at 200 chars) |
| `tool.Skill.p.args` | `args` | string | no | free text passed through to the skill | missing |

## Rules

| id | rule | noeta today |
|---|---|---|
| `tool.Skill.1` | A skill is a packaged instruction set the user or project set up for one kind of task (deploy steps, review checklist, repo workflow). | covered |
| `tool.Skill.2` | Available skills are listed, one line each, in a message in the conversation (the `role: "system"` message), not in the tool definition. | covered |
| `tool.Skill.3` | When a listed skill covers the task, call this tool first; its instructions then replace the default approach for the turn. | covered |
| `tool.Skill.4` | Some skills run in a sub-agent and return the finished result instead of loading instructions. | missing (no `context: fork`) |
| `tool.Skill.5` | A skill that runs in the background answers with just an agent name; the outcome comes later in a task notification, and the model neither waits on it nor calls the skill again in the meantime. | missing |
| `tool.Skill.6` | A user naming a skill, or typing `/<name>` ("slash command"), is a request to invoke it. | covered |
| `tool.Skill.7` | `skill` is the exact listed name without a leading slash. | covered (slash not mentioned) |
| `tool.Skill.8` | Plugin skills are addressed as `plugin:skill`. | missing |
| `tool.Skill.9` | Skills scoped to a directory carry a path prefix (`apps/web:deploy`); when a scoped and an unscoped skill share a name, choose the scope that contains the files in hand, the deepest match first, falling back to the unscoped one. | missing |
| `tool.Skill.10` | Only listed names, or names the user typed explicitly, are valid. | covered |
| `tool.Skill.11` | Built-in CLI commands (`/help`, `/clear`, …) are not skills. | missing |
| `tool.Skill.12` | If a `<command-name>` block is already in the current turn, the skill is already loaded: follow it, do not call the tool again. | missing (noeta says "do not re-activate", no `<command-name>` signal) |
| — | noeta-only: the call must be alone in its response, otherwise it is not loaded. | contradicts — the baseline sets no such restriction |
| — | noeta-only: a listing line may be shortened; activating loads the whole skill. | n/a (true of the baseline too, just unstated) |

## Result shape

| id | shape (observed) |
|---|---|
| `tool.Skill.r.1` | Not observed as a tool call; in m1 the skill was started by the user's `/release-notes v0.1`. |
| `tool.Skill.r.2` | A user-invoked skill appears in the user message as a `<command-message>`/`<command-name>`/`<command-args>` text block followed by a separate text block: `Base directory for this skill: <dir>`, a blank line, the SKILL.md body without frontmatter, then `Arguments: <args>` when args were given. That body block carried the message's cache breakpoint. See `reminders.md`. |

## Variants

Same hashes in every session.
