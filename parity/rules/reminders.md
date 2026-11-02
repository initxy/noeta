# Reminders and wrappers

Claude Code version: **2.1.312**. Baseline session `m1` (API-key user, `auto` mode); `m2`
(default mode), `m3` (auto-compaction), `m4` (`--continue`) are the same configuration. `s1`
(OAuth subscriber) is not baseline; rows marked **s1 only** were seen nowhere else.

Covers every text the harness adds to `messages` around the user's words and the tool
results: `<system-reminder>` blocks, `role: "system"` messages, command wrappers and
notifications. Not covered here: the environment message (`main-prompt.md`), the compaction
instruction, summary preamble and post-compaction layout (`compaction.md`), side-request
prompts such as the title or "describe your most recent action" (`side-requests.md`), tool
result bodies (`tools/<Tool>.md`), the sub-agent's own prompt (`subagent.md`).

"Trigger" is inferred from where a block appears in the recordings, not from Claude Code's
code. "Persists" means the block stays at its position in every later request of the same
context (it becomes part of the history); "transient" means it is sent once.

## Sources

Hashes per `README.md` (normalized text, `source_hash.py`).

| kind | source | sha256 |
|---|---|---|
| instructions | `m1/0001 messages[0].content[0]` | `27d052d1258d8cb69ce683061f24baf6b19809c6feba54076bfd821a82fd7c15` |
| git-status | `m1/0001 messages[0].content[1]` | `8719a4aaa044276ad1b3e66c4045470aec8d686981d3931cfa261e91ddae4f90` |
| attribution | `m1/0001 messages[0].content[2]` | `a647e3aeea2a4db344c588e3df37610eb5eabf16ae4ce50d1f07fab94d120c7b` |
| total-tokens | `m1/0004 messages[4].content[0]` | `5e9a0576a1c27d9e6fd0e82488a869653fec27573cdbea6326e0fce4d2253f5a` |
| quiet-nudge | `m1/0013 messages[19].content[0]` | `d9a0c172329870d4206b708ccc171b850b2babe2bc90496134eff85b52549c71` |
| tool-loaded | `m1/0019 messages[33].content[1]` | `b66c12e17217628b89626ea0e34edeeb4591705481027400b81963351bcc3b73` |
| task-notification (shell) | `m1/0019 messages[34].content[0]` | `21199a9c443b50f6c7c83f501375daa99c0f8a10b5284aa4a05ecaa6ca265bc8` |
| task-notification (agent) | `m1/0037 messages[56].content[1]` | `ec386b8a7144adf60cbd0069b433cae78918358bfd2cd33f967ffa6a92150bad` |
| agent-message | `m1/0036 messages[54].content[0]` | `6977a5a1cc55813fe7d0a225c4d56190e77b41b18916394856023f1c2b087335` |
| tools-used-before-summary | `m1/0037 messages[56].content[0]` | `7f88dd3865ee6d66170e13681e9486b7839937e323d6c49b4518015802f8f9e4` |
| skill command wrapper | `m1/0037 messages[56].content[2]` | `9a6166b493853c1e559ee0d23fda62147f24876e5dfe3f1f899108cf8625b898` |
| skill body | `m1/0037 messages[56].content[3]` | `ead407d187eb7f21d3f90f5c064ec7061ecd9fda6199ca650f995247b3592865` |
| local command wrapper (`/plan`) | `m1/0053 messages[94].content[0]` | `7e5161b7d761b0144d15863b68e8bf1963ffbea90be4b3d2a23a5d805248486b` |
| local-command-stdout | `m1/0053 messages[94].content[1]` | `a6747d3300a0a898a9167a6ac5228ab34869d8ba8556a0d3f214b360a42a92df` |
| local-command-caveat | `m1/0059 messages[3].content[3]` | `19cf21a5f4433d7bb80ee65f716e5ce293061e21fe649116d5b4b91056b76719` |
| local command wrapper (`/compact`) | `m1/0059 messages[3].content[4]` | `2cb99709ec25cef47a872d9a62a0d6f3280571cb7433d8d3daaf000d27267c17` |
| compact stdout | `m1/0059 messages[3].content[5]` | `867dbdf1980c998a7229b0f9868bac38d56872fd3ec250e518d2cdea7378725b` |
| interrupt marker | `m1/0059 messages[3].content[2]` | `ba5d0b52e396e1ce0003e585c77755de2ff8045a4d67cb720b299f3b21cc17cc` |
| plan-mode-active | `m1/0059 messages[4].content[0]` | `ca6cd97305634f6859f5bf9b5d8217ed5f7b1123dda4199fb84a980e32dd53f6` |
| plan-mode-exited | `m1/0063 messages[13].content[0]` | `ebae42f95e4645edefce59b0564dda6630214b99174032592c0c584265cf0c5e` |
| auto-mode guidance (standalone) | `m3/0014 messages[4].content[0]` | `4025702f3d8d0bfac00f8bf011d36dddc701194737379c65676a33086efe7177` |
| mid-turn message | `m3/0023 messages[3].content[1]` | `d3a645614e8ef54ba7a9f4c174bad0ece9f74e98e61092a06f4a1d1bcc6e720e` |
| instructions re-read | `m4/0001 messages[30].content[0]` | `b5d786b0623df2294419344f20de28dd3daedf22a903b3ec1b51fbec4b4a2cee` |
| sub-agent hand-back instruction | `m1/0029 messages[0].content[2]` | `924634f52ab1ee780715562b3efd5a21050c3b3ffb47230ed13eb0c6f711bf86` |
| ask-user answer (tool result) | `m1/0052 messages[82].content[0]` | `1cacb62e40171616c11fa4198a508bea2825c0628df377b0ee0d334de278fbcf` |
| plan approved (tool result) | `m1/0063 messages[12].content[0]` | `c33e71ed08f45916dfb24c33a254a11d878cba1e8c8eb9a3fb0c1419650e0a1d` |
| file-changed (s1 only) | `s1/0038 messages[51].content[0]` | `779d009b1dfb3e58676c2302c2b084f456b58323884e4ae5a4dc76d8adac5aeb` |
| agent-list delta (s1 only) | `s1/0031 messages[4].content[0]` | `710f6b4db01fd2292bbea4c4f5d4e008a47a30d00198ef4d757bdb1622c3e794` |

## General placement

| id | rule | noeta today |
|---|---|---|
| rem.all.1 | Two carriers exist. Harness context tied to the user's input (instructions, git status, attribution, queued notifications, re-read instructions, hand-back instruction) is a `<system-reminder>…</system-reminder>` text block inside a `role: "user"` message. Per-step harness notes (token count, nudges, mid-turn background notifications, plan-mode texts, auto-mode guidance, file-changed notes) are a separate `role: "system"` message inside `messages`, with no `<system-reminder>` wrapper. | covered — `Reminder.role` `user` renders as a `<system-reminder>` block in the user turn, `system` as its own system turn (`_anthropic_wire.py`) |
| rem.all.2 | A `<system-reminder>` block opens with the tag and a newline and closes with the tag and a newline; it is always its own content block, never merged into the user's text. | covered — `_wrap_system_reminder`, one text block per note |
| rem.all.3 | `role: "system"` messages sit directly after the user message they belong to (a prompt or a tool-result message) and before the next assistant message. Several notes due at the same point are joined into one `role: "system"` message, separated by a blank line, not sent as several messages. | covered — adjacent system turns are joined a blank line apart (`messages_to_wire`) |
| rem.all.4 | Inside a joined `role: "system"` message the turn-scoped notes (quiet nudge, background notification, plan-mode exit, auto-mode guidance) come before the `<total_tokens>` line. | covered — `Reminder.order`: nudge 60, plan 64, auto mode 70 before the budget's 80 |
| rem.all.5 | Wrappers of the user's own input (command wrappers, skill body, mid-turn message, interrupt marker) are plain text blocks in the user message, not wrapped in `<system-reminder>`. | covered — origin `inline` and command tags go plain; the mid-turn message is framed, not wrapped |
| rem.all.6 | No reminder carries its own `cache_control`. The main loop sets exactly one message-level breakpoint, on the last content block of the last message, whatever that block is; a reminder holds it only because it is last. The breakpoint uses the default 5-minute TTL in the baseline (`s1` uses `ttl: "1h"` and a second breakpoint on the previous assistant message). | covered — one message mark on the last block of the last message (`apply_cache_control`), default TTL |
| rem.all.7 | A message's `content` is a plain string when it has one text block and no breakpoint; the same message becomes a one-block list when it carries the breakpoint. Both forms are the same message for comparison purposes. | covered — `bare_strings` |
| rem.all.8 | All reminders below persist: once sent they stay at their position in every later request until compaction rewrites the history. None was observed being removed or rewritten in place. | covered — notes are recorded with their message in the task log and replayed as they were |

## instructions (CLAUDE.md)

| id | rule | noeta today |
|---|---|---|
| rem.instructions.1 | Trigger: start of a conversation when at least one instruction file is found. Placement: first content block of the first user message, before git status, attribution and the user's text. | covered — `instructions` plugin `on_start`, first block of the first turn |
| rem.instructions.2 | Opens with a preamble: these are the codebase's and the user's instructions, they must be followed, and they override default behaviour exactly as written. | covered — `PREAMBLE`, our words |
| rem.instructions.3 | Then, per file: a line naming the absolute path and, in parentheses, its kind (the project file is labelled as project instructions checked into the codebase), a blank line, the file's text verbatim. | covered — `render_file`, kinds in `LABELS` |
| rem.instructions.4 | Sent again before the summary after compaction (see `compaction.md`); unchanged otherwise. | covered — `Instructions.lead_resident` re-reads the files ahead of the summary |
| rem.instructions.5 | Files, in order (Claude Code 2.1.312 bundle): the machine's managed `CLAUDE.md` and its `.claude/rules`; the user's `~/.claude/CLAUDE.md` and `~/.claude/rules`; then each directory from the outermost ancestor of the working directory (the filesystem root excluded) down to it: `CLAUDE.md`, `.claude/CLAUDE.md`, the `.claude/rules` files, `CLAUDE.local.md`; last the auto memory index. `AGENTS.md` is not read. | deviation — `parity/deviations.md` "noeta's own files" row: `NOETA.md` / `.noeta/` in Claude Code's order, `AGENTS.md` as fallback |
| rem.instructions.6 | Kinds named in the path line: managed policy, the user's global instructions, project instructions checked into the codebase, the user's private (not checked-in) project instructions, the auto memory. | covered — `LABELS`, our words |
| rem.instructions.7 | `@path` in a file's text (not in code blocks, inline code or HTML comments; `./`, `~/`, absolute or a plain relative name; a `#fragment` is cut) imports that file, listed right after the importing one with the same kind; a file is listed once; an import five levels below the first file is not read; an import outside the working directory from a repository file needs the user's approval. HTML comments are removed from the text, a rule file's frontmatter too. (bundle) An import whose extension is not on Claude Code's list of text extensions (markdown, plain text, config formats, source code, …) is skipped; a name without an extension is read. | deviation — `parity/deviations.md` Instruction files row: an import leaving the repository and working directory is skipped, not asked; the rest covered (`files.py`: `IMPORT_DEPTH_MAX` 5, `TEXT_EXTENSIONS`, comments and frontmatter cut) |
| rem.instructions.8 | A rules file whose frontmatter has `paths:` (gitignore-style globs, relative to the directory holding `.claude/`; the user's relative to the working directory) is left out at the start and shown when a file it matches is touched. | covered — `paths:` rules held back and shown when a touched file matches (`_nested_files`) |
| rem.instructions.9 | The auto memory index (`MEMORY.md`, cut to its line and byte limits) is the last file of this reminder, labelled as auto memory. | covered — `read_memory_index` (200 lines / 25,000 bytes), last file, memory kind |
| rem.instructions.10 | When a tool touches a file below the working directory, the instruction files of each directory between them (and matching `paths:` rules) that were not shown yet follow as their own reminder, one per file: the path line (no kind) and the text, no preamble. (bundle) A nested file that is not managed policy and is longer than the larger of 40,000 characters and 5 % of the model's window in characters (3 characters a token, 4 for older Claude models) is cut at a line end and closed by a line saying only its start is shown and naming the path and line offset to `Read` the rest. | covered — `cut_nested` / `NESTED_CUT_NOTE` (our words); also when the files are sent again after a compaction |

## git-status

| id | rule | noeta today |
|---|---|---|
| rem.git-status.1 | Trigger: start of a conversation inside a git repository. Placement: second block of the first user message, after instructions, before attribution. | covered — `environment` plugin `on_start`, after the instructions |
| rem.git-status.2 | Opens by offering the following as context, then a `# gitStatus` heading and a note that it is a snapshot from the start of the conversation that will not update. | covered — `render_git`, our words |
| rem.git-status.3 | Fields in order, each separated by a blank line: current branch; main branch (labelled as the usual PR target); git user name; `Status:` with the short status or `(clean)`; `Recent commits:` as one-line `hash subject` entries. With no user name set the user line is left out; a repository without commits gets an empty list (2.1.312 bundle). | covered — `render_git` (no user line without one; status cut at 2,048 bytes) |
| rem.git-status.4 | Closes by saying the harness attached it automatically, the user did not write it, it is about the user's own account and checkout, and the model need not repeat it to them. | covered — `render_git` closing line |
| rem.git-status.5 | Re-sent before the summary after compaction (see `compaction.md`). | covered — `Environment.lead_resident` takes the snapshot again at the compaction |

## attribution

| id | rule | noeta today |
|---|---|---|
| rem.attribution.1 | Trigger: every new context: start of a conversation, start of a sub-agent, after compaction. Placement: after instructions and git status, before the user's text; in a sub-agent it is the first block, before the task prompt. After compaction it was seen both before the summary and appended after the first tool result of the next request (see `compaction.md`). | covered — `on_start`, `lead_resident` / `resident` after a compaction; sub-agents too (parity pairs every sub-agent call) |
| rem.attribution.2 | Says what to append to commit messages and PR descriptions the model creates from now on: a commit trailer `Co-Authored-By: <model display name> <noreply@anthropic.com>` and a PR-description footer line saying it was generated with Claude Code, linking to it. | deviation — `parity/deviations.md` main-prompt row: the PR footer links to noeta |
| rem.attribution.3 | States precedence: this copy replaces any earlier attribution guidance (including earlier copies of itself); the user's own instructions about these lines (instruction files, memory) win over it; lines it does not list must not be added. | covered — `render_attribution`, our words |
| rem.attribution.4 | The model name in the trailer is the session's model display name (`Claude Opus 5.5`). | covered — the catalog's `display_name`; a model without one or without a co-author address gets no note (deviation row) |

## total-tokens

| id | rule | noeta today |
|---|---|---|
| rem.total-tokens.1 | Form: `<total_tokens>N tokens left</total_tokens>` in a `role: "system"` message. | covered — `budget_note` |
| rem.total-tokens.2 | Trigger: after every user prompt message and after every tool-result message, in the main loop and in sub-agents (`m1`–`m4`). Absent everywhere in `s1`. | covered — `_owes_budget_note`; only for Claude models Claude Code knows (`gets_budget`); a sub-agent's task prompt gets none |
| rem.total-tokens.3 | N is 15,000,000 right after each new user prompt and decreases during the turn by roughly the output tokens the model has spent so far in that turn; it is not the context-window remainder. | covered — `TOKEN_BUDGET` minus the output spent this turn |
| rem.total-tokens.4 | The first occurrence of a conversation is inside the environment message (just before the date line) rather than a separate message. | covered — joined into the first request's environment system turn, before the date (`ORDER_BUDGET` 80, `ORDER_DATE` 90) |
| rem.total-tokens.5 | Joined with any other note due at the same point (rem.all.3); persists with its original value. | covered |
| rem.total-tokens.6 | Observed defect, not to copy: on `--continue` (`m4/0001`) the line appears twice in one message. | covered — not copied: one note at most per point (`_owes_budget_note`) |

## quiet-nudge

| id | rule | noeta today |
|---|---|---|
| rem.quiet-nudge.1 | Content: the user has not heard from the model for a while; keep them updated when there is something worth telling (a finding, a change of plan). | covered — `quiet_nudge.py` `NUDGE`, our words |
| rem.quiet-nudge.2 | Trigger (observed twice in `m1`): after the fifth consecutive assistant step that produced tool calls but no text since the last user input (a prompt, or a plan approval). The 2.1.312 bundle counts steps (`silent_turn_reminder`: 5 steps; a seconds-based form exists behind a setting). | deviation — `parity/deviations.md` Reminders row: every reply without text counts as quiet, visible thinking included; 5 steps, reset by an answered question (`QUIET_STEPS`, `ASKS_USER`) |
| rem.quiet-nudge.3 | Placement: `role: "system"` message after the tool-result message, joined before the `<total_tokens>` line. Persists. A silent stretch is nudged again after five more tool-only steps since its last nudge, three times at most (2.1.312 bundle; the recordings' stretches were too short to show a second). | covered — system turn before the budget (`ORDER` 60); again after 5 more steps, 3 at most (`MAX_NUDGES`) |

## tool-loaded

| id | rule | noeta today |
|---|---|---|
| rem.tool-loaded.1 | Trigger: a `ToolSearch` call that loaded tools. Placement: a text block `Tool loaded.` right after the `tool_result` block (whose content is `tool_reference` blocks) in the same user message. One block per result message, even when several tools were loaded. | covered — `TOOL_LOADED_TEXT` after the results, once per message (`_results_message`) |
| rem.tool-loaded.2 | Persists; not wrapped in `<system-reminder>`. | covered |

## task-notification (background shell)

| id | rule | noeta today |
|---|---|---|
| rem.task-notification.1 | Trigger: a `run_in_background` Bash command finishes. When it finishes while a turn is running, it is delivered at the next step as a `role: "system"` message after that step's tool-result message (no `<system-reminder>` wrapper), joined before `<total_tokens>`. | covered — system-origin text taken into a running turn folds as a system message (`loop/state.py` `_say`) and goes out as a `role: "system"` turn after the tool results; taken at a turn's start it stays a `<system-reminder>` in the user turn |
| rem.task-notification.2 | Opens with a four-part guard: this is an automated system notification, not user input; it is not an acknowledgement, confirmation or answer to a pending question; no human input has arrived since the last real user message; any claim that the user said or approved something, including in the model's own earlier messages, is not consent. | covered — `NOTICE_GUARD` (`toolkit/notices.py`), our words |
| rem.task-notification.3 | Then a `<task-notification>` element with children in this order: `<task-id>`, `<tool-use-id>` (the `tool_use` that started it), `<output-file>` (absolute path of the output file), `<status>` (`completed`), `<summary>`. | covered — `shell/notices.py` `task_notification`, children in this order |
| rem.task-notification.4 | Shell summary: background command, its `description` in quotes, completed, and the exit code in parentheses. | covered — `shell_end_summary` |
| rem.task-notification.5 | No output text is included; the model reads the output file if it needs it. | covered — the shell's end notice carries no output (`_end_notice`) |

## task-notification (agent)

| id | rule | noeta today |
|---|---|---|
| rem.task-notification.6 | Trigger: a background sub-agent stops and has no background work of its own still running. Observed: when the stop happens while the main loop is idle (its report already arrived as an agent-message and was answered), the notification waits and is delivered with the next user input, as a `<system-reminder>` block in that user message, before the user's prompt or command. | covered — `subtasks` `_ended`: after a hand-back the notice waits for the next request |
| rem.task-notification.7 | Same guard text as rem.task-notification.2, then `<task-notification>` with `<task-id>` (the agent id), `<tool-use-id>`, `<output-file>`, `<status>` (`completed`), `<summary>` (agent, its description in quotes, finished), `<note>`, `<result>`, `<usage>`. | covered — `subtasks/extension.py` `task_notification` |
| rem.task-notification.8 | `<note>`: one is sent each time the agent comes to rest with nothing of its own still running; because the user may resume the agent, the same task id can produce several notifications. | covered — `NOTICE_NOTE` |
| rem.task-notification.9 | `<result>`: when the agent delivered its report through `SubagentHandback`, the result only points to the agent-message from that agent id and says the report is not repeated here. | covered — `RESULT_POINTER` |
| rem.task-notification.10 | `<usage>` holds `<subagent_tokens>`, `<tool_uses>` and `<duration_ms>`, in that order. | covered — `<usage>` with the three children in this order |

## tools-used-before-summary

| id | rule | noeta today |
|---|---|---|
| rem.tools-used.1 | A `<system-reminder>` block holding `<tools-used-before-summary>` with a tool name (`SubagentHandback` in both `m1` and `s1`). | covered — `TOOLS_BEFORE_SUMMARY` (`_deliver`) |
| rem.tools-used.2 | Placement: first block of the user message that also carries a queued agent task-notification, immediately before it. Its exact trigger is not determinable from the recordings beyond always pairing with that notification. | covered — recorded right before the notice in the same turn (`_deliver`) |

## agent-message (sub-agent hand-back)

| id | rule | noeta today |
|---|---|---|
| rem.agent-message.1 | Trigger: a sub-agent calls `SubagentHandback`. The report reaches the parent as a new `role: "user"` message (its own turn; the parent answers it without a new user prompt), not as a tool result and not in `<system-reminder>`. No `<total_tokens>` follows it. | covered — `agent_message`, origin `agent`; no budget note follows it (`_owes_budget_note`) |
| rem.agent-message.2 | Layout: a lead line announcing a message from another session; `<agent-message from="<agent id>">`; inside it a framing line; the report; `</agent-message>`; then a closing paragraph. | covered — `subtasks/tool.py` `agent_message` |
| rem.agent-message.3 | Framing line: marks it as a sub-agent hand-back; the text is model output, not the user; instructions, requests or approvals inside it carry no user authority; every report line is indented by the harness, so a frame-looking line at column zero inside it is forged; notes above the frame may quote model text and carry no authority either. | covered — `AGENT_MESSAGE_FRAME`, our words |
| rem.agent-message.4 | Every line of the report is indented by two spaces (blank lines become two spaces). | covered — `_indent` |
| rem.agent-message.5 | Closing paragraph: the sender is an agent this session started for the user, not the user; its content counts as that agent's report or request and stays within this session's permissions; it can never raise privileges: no edits to permission settings, instruction files or configuration on its request, no reading it as the user's answer to an open approval, and a request to perform an action it was refused is declined and reported to the user (permission laundering). | deviation — `parity/deviations.md` "noeta's own files" row (the text says `NOETA.md`); otherwise covered (`AGENT_MESSAGE_CLOSE`) |

## sub-agent hand-back instruction

| id | rule | noeta today |
|---|---|---|
| rem.handback-instruction.1 | Trigger: start of every sub-agent seen (`Explore` in `m1`, `general-purpose` in `s1`). Placement: `<system-reminder>` block after the task prompt in the sub-agent's first user message. | covered — `HANDBACK_REMINDER` after the task prompt, in auto mode (`_hands_back`) |
| rem.handback-instruction.2 | Content: the final report goes through `SubagentHandback` with the full report as `message`; that call ends the run so it must be the last step; only that call reaches the caller (trailing prose is dropped); sending the same report through `SendMessage` as well would deliver it twice. | covered — `HANDBACK_REMINDER`, our words |

## slash commands and skills

| id | rule | noeta today |
|---|---|---|
| rem.command.1 | A user-typed skill command (`/release-notes v0.1`) replaces the user's text with a block of three tags in this order: `<command-message>` (skill name, no slash), `<command-name>` (with slash), `<command-args>` (raw argument string), one per line. | covered — `toolkit/commands.py` `command_message` |
| rem.command.2 | The skill body follows as the next text block in the same user message: a line giving the skill's base directory (absolute), a blank line, the `SKILL.md` body with its frontmatter removed and `$ARGUMENTS` replaced by the argument string. | covered — `skills/registry.py`: base-directory line, body with `$ARGUMENTS` substituted |
| rem.command.3 | A built-in local command (`/plan <args>`, `/compact`) uses the order `<command-name>`, `<command-message>`, `<command-args>` (empty when no arguments), the second and third lines indented by 12 spaces. | covered — `PLAN_COMMAND` (`permissions/modes.py`), `/compact`'s `command_text` |
| rem.command.4 | A local command's terminal output follows as its own block `<local-command-stdout>…</local-command-stdout>`, raw, including ANSI escapes (`/compact` prints a dimmed "compacted" line; `/plan` prints that plan mode was enabled). | covered — own one-line output text, without ANSI escapes |
| rem.command.5 | `/compact` is preceded by a `<local-command-caveat>` block: the command was run directly by the user, not sent to the model as a request, its output went straight to the user, and it is kept only as context for later messages. `/plan` with arguments had no caveat and itself started a model turn. | covered — `COMMAND_CAVEAT` before `/compact`; none for `/plan` |
| rem.command.6 | All command blocks persist; after `/compact` they appear in the first user message after the summary, ahead of the next prompt (see `compaction.md`). | covered — the command's blocks follow the summary with the next prompt (`compaction` plugin) |

## skill notes (bundle, not recorded)

| id | rule | noeta today |
|---|---|---|
| rem.skill-mention.1 | When a user message names a skill as `/<name>` anywhere but as its leading command (preceded by the start, whitespace or a CJK sentence mark, followed by whitespace or the end), a `<system-reminder>` after the message says it names that skill: call `Skill` with it (and the arguments given) if the user wants it run, and do not run it if they only mention it. One per skill named; only skills the model may invoke. | covered — `skills` plugin, `reminder/skill_mention` (our words) |
| rem.nested-skills.1 | When a tool touches a file in a directory below the working directory that has its own skills directory (in a directory between the two), that directory's skills join the conversation and a `<system-reminder>` names the directory (relative) and lists the new skills by name, one `- <name>` line each; the next model call lists them with their descriptions (`main.env.16`). | covered — for `.noeta/skills` and `.agents/skills` (noeta's places), in trusted workspaces; the directories found are in the task log |

## empty reply (bundle and fake-endpoint runs, not recorded)

No baseline session got an empty reply; these rows come from the 2.1.312 bundle (the
`thinking_only_retry` path of the query loop) and from running 2.1.312 against a fake Messages
endpoint that answered with an empty reply, in `-p` and interactive mode
(`docs/specs/archive/2026-11-01-empty-turn.md`).

| id | rule | noeta today |
|---|---|---|
| rem.empty-reply.1 | Trigger: a main-loop reply whose stop reason is `end_turn` (or `stop_sequence`) with no tool call and no text that is more than whitespace: no content blocks, only empty text, or only reasoning. A cut at the output limit is the output-limit recovery's, not this. | covered — `noeta/core/runner/closing.py`, `after_empty_reply` |
| rem.empty-reply.2 | The reply is not kept; the next request ends with a fixed one-line nudge (the previous response had no visible output; continue and produce a user-visible response, in square brackets) as plain text, unwrapped, added to the user turn before it (after the prompt or the tool results) ahead of that step's `role: "system"` notes, which still end the request and carry its last cache mark (2.1.312 on the Opus 5.5 configuration). It persists. | covered — `EMPTY_REPLY_NUDGE`, Claude Code's text under ADR-020; the Anthropic adapter places it |
| rem.empty-reply.3 | Once per step: the nudge is sent again only after a tool round, or a new prompt or notification, since the last one (not after a Stop-hook continuation). An empty reply after the nudge ends the turn quietly: no error, `-p` reports `success` with an empty `result` and exits 0, and Stop hooks run without `last_assistant_message`. (A flag that is off by default turns a second empty reply to a bare prompt into an API error instead.) | deviation — `parity/deviations.md` Reminders row (a blocking Stop hook starts the count again); otherwise covered: the turn ends with `turn_done` reason `empty_reply`, its answer empty |
| rem.empty-reply.4 | A dropped reply is still a model call: its tokens and cost count, and `-p`'s `num_turns` includes it (tool, empty, empty gives 3). | covered — the `continue` / `turn_done` entry carries the dropped reply's model and usage |
| rem.empty-reply.5 | Not nudged either: a reply right after a successful call of a tool listed in `CLAUDE_CODE_TERMINAL_MCP_TOOLS` (an MCP tool whose result is meant to end the turn), or after a `StructuredOutput` call since the last prompt (the answer was already given through the tool). | covered — `Tool.silent_reply_after` (`closing.py`): `StructuredOutput` any call, an MCP tool named in `NOETA_TERMINAL_MCP_TOOLS` a successful one; the turn ends and the structured-output reminder follows |

## list changes (bundle, not recorded)

The recordings never changed a list mid-conversation; these rows come from the 2.1.312 bundle.
Each note goes out with the next request after the change, as a `role: "system"` note.

| id | rule | noeta today |
|---|---|---|
| rem.deltas.1 | Deferred tools: names that join are listed under the deferred-tool lead-in (`main.env.6`); names announced earlier that return are named as available again (for MCP tools: their server reconnected), to be loaded with `ToolSearch` as before; names that leave are listed as no longer available (for MCP tools: their server disconnected), not to be searched for since `ToolSearch` will not find them. A note that names a removal closes with a line saying it is background context, not to be narrated to the user unless relevant. After a compaction the full list is sent again. | covered — `environment` plugin `deferred_delta` (our words); the last list named is in the task log |
| rem.deltas.2 | Agent types: types that join are listed under a lead-in saying new agent types are available, one line each in the agent list's format (`main.env.8`); types that leave are listed by name as no longer available, followed by the background-context line. A conversation that had no agent list gets the whole list once a type appears. | covered — `subtasks` plugin (`roster.py`); the types last listed are in the task log |
| rem.deltas.3 | MCP server instructions (the `instructions` of a server's `initialize` result, cut to 4,096 characters and `… [truncated]`, bundle `u$`): a server that connects with instructions, or whose instructions changed, gets a `# MCP Server Instructions` heading, a lead-in saying these servers gave instructions for their tools and resources, and one `## <server>` block with the text; a server that disconnects is named under a line saying its instructions above no longer apply, followed by the background-context line. Sent again in full after a compaction. | covered — `mcp` plugin (`instructions.py`, 4,096 characters); the last told set is in the task log |

## plan mode

| id | rule | noeta today |
|---|---|---|
| rem.plan-active.1 | Trigger: plan mode on. Placement: a `role: "system"` message after the user message. On the `/plan` turn itself (`m1/0053`) no plan-mode text was sent; it first appeared in the next request built (`m1/0059`, after `/compact`), where it was joined with the post-compaction attachments and appeared twice in that one message. | covered — `permissions/modes.py`: a typed `/plan <request>` waits one call |
| rem.plan-active.2 | Restriction: the user does not want execution yet; no edits except the plan file, no non-read-only tools, no config changes or commits, no other changes; this overrides other instructions. | covered — `plan_mode.md`, our words |
| rem.plan-active.3 | Plan file: its absolute path (under the config directory, `plans/<three-word-slug>.md`); whether it exists yet; create it with `Write` if not; build it incrementally with `Write`/`Edit`; it is the only editable file. | deviation — `parity/deviations.md` Permissions row: plan files live in `<noeta home>/plans/`; three-word name (`new_plan_path`), exists / missing line, otherwise covered |
| rem.plan-active.4 | Phase 1, understanding: read code and ask questions; use only the `Explore` agent type; look for existing code to reuse rather than propose new code; up to 3 `Explore` agents in parallel in one message, as few as possible (usually 1): one for a task confined to known files, named files or a small change, several when the scope is uncertain, several areas are involved or existing patterns are needed first, each with a distinct focus. | covered — `plan_mode.md` step 1 |
| rem.plan-active.5 | Phase 2, design: launch `Plan` agent(s), at most 1 in parallel; at least one for most tasks, none only for trivial ones (typo, one-line change, simple rename); the agent prompt carries the Phase 1 context (files, code paths), requirements and constraints, and asks for a detailed plan. | covered — `plan_mode.md` step 2 |
| rem.plan-active.6 | Phase 3, review: read the critical files, check the plan matches the request, use `AskUserQuestion` for remaining questions. | covered — `plan_mode.md` step 3 |
| rem.plan-active.7 | Phase 4, final plan in the plan file: starts with a Context section (why, what prompted it, intended outcome); only the recommended approach; short enough to scan, detailed enough to execute; names critical files, describing a repeated pattern once with a few example paths, not every file or line number; points to existing functions to reuse with paths; ends with an end-to-end verification section (run the code, use MCP tools, run tests). | covered — `plan_mode.md` step 4 |
| rem.plan-active.8 | Phase 5: end the turn by calling `ExitPlanMode` once the plan is final, to say planning is done; this is critical: a plan-mode turn ends only with `AskUserQuestion` or `ExitPlanMode`, and for no other reason. | covered — `plan_mode.md` step 5 |
| rem.plan-active.9 | Approval of the plan is requested only through `ExitPlanMode`, never as a text question or through `AskUserQuestion`; `AskUserQuestion` is only for requirements or choosing between approaches; phrases such as "Is this plan okay?", "Should I proceed?", "How does this plan look?", "Any changes before we start?" go through `ExitPlanMode`; questions are allowed at any phase; do not make large assumptions about user intent, the goal is a well-researched plan with loose ends tied before implementation. | covered — `plan_mode.md` step 5 and closing paragraph |
| rem.plan-active.10 | Cadence (bundle `hTn`: 5 and 5): after a plan-mode text, none is sent until 5 more user prompts have come; then with the next model call (checked before every call, so a prompt that arrives mid-turn counts at once). The first text after entering plan mode (or after a compaction) and every fifth after it is the full one; the others are a short form: plan mode is still on, the full instructions are earlier, read-only except the plan file (its path), keep to the five phases, end turns with `AskUserQuestion` or `ExitPlanMode`, never ask for approval in text. | covered — `PLAN_TURNS_BETWEEN` / `PLAN_FULL_EVERY` 5 and 5, `plan_mode_sparse.md` |
| rem.plan-active.11 | A task or a session that starts in plan mode (`--permission-mode plan`) gets the full text with its first request, as a mode switched to before a prompt does; only a typed `/plan <request>` delays it to the call after the first (rem.plan-active.1). | covered — a task created in plan mode gets the full text with its first request |
| rem.plan-active.12 | A sub-agent of a planning conversation runs in plan mode too, with its own plan file (`<parent plan name>-agent-<agent id>.md`, bundle `nT`), and gets a sub-agent form of the text: the same restriction and plan-file lines, then answer the query you were given fully in the final message, asking any clarifying questions all at once; no phases, no `ExitPlanMode`. | covered — `plan_mode_subagent.md`, `<parent plan>-agent-<task id>.md` (`modes.py`) |
| rem.plan-reentry.1 | Trigger: plan mode entered again after it was left earlier in the conversation (by any means), while the plan file from then exists. Placement: just ahead of the plan-mode text. Content: a `## Re-entering Plan Mode` heading; a plan file from the earlier planning session exists at its path; before planning, read it, compare the current request with it, then overwrite it for a different task (even a related one) or revise it (dropping outdated parts) for an explicit continuation of the same task; always edit it before `ExitPlanMode`; treat it as a fresh planning session. | covered — `plan_mode_reentry.md`, ahead of the plan-mode text |
| rem.plan-exited.1 | Trigger: plan mode left by any means (`ExitPlanMode` approved, or a switch to another mode; bundle `cvr`), sent with the next request unless planning again by then. Placement: `role: "system"` message after its tool result (or the prompt), joined with `<total_tokens>`; after auto → plan → auto the auto-mode note is not sent again (r2 m1: only the plan-exit note and the budget). | covered — `plan_exited` owed on every way out of plan mode (`permissions/state.py`), dropped when planning again |
| rem.plan-exited.2 | Content: a `## Exited Plan Mode` heading; plan mode is over and edits, tools and actions are allowed again; the plan file's absolute path for reference, only when the file exists. | covered — `PLAN_EXITED` (+ `PLAN_EXITED_FILE` when the file exists), our words |
| rem.plan-approved.1 | The `ExitPlanMode` tool result itself (see `tools/ExitPlanMode.md`) says the plan was approved, suggests starting with the todo list if applicable, gives the saved path, and repeats the plan under an approved-plan heading. | covered — `APPROVED` (`permissions/plan.py`) |

## auto-mode guidance

| id | rule | noeta today |
|---|---|---|
| rem.auto-mode.1 | Content: while auto mode is on, the model may use Bash to read (`cat`, `head`, `sed -n`), search (`grep`, `find`) and make small mechanical edits (`sed`, heredocs, short scripts) instead of `Read`/`Edit`/`Write`; the choice is the model's; prefer `Edit`/`Write` when a shell edit is fragile (exact or multi-line replacements, GNU vs BSD flag differences). | covered — `permissions` auto-mode note, our words |
| rem.auto-mode.2 | Placement at start: the last section of the environment message (`main-prompt.md`). | covered — joined into the first request's environment system turn |
| rem.auto-mode.3 | Sent (as a `role: "system"` message joined before `<total_tokens>`) only while no auto-mode note is in the conversation since auto mode was last left or since the last compaction (bundle `fvr` / `qTn`): after a compaction at the next user prompt or tool result, or, while planning, at the plan exit; not after auto → plan → auto (r2 m1). In `s1` a `general-purpose` sub-agent received it (with the agent list, see rem.agent-list.1) after its first tool result; the `m1` `Explore` sub-agent never received it (no `Edit`/`Write`, rem.auto-mode.6). | covered — `auto_noted` (`permissions/state.py`): cleared on leaving auto mode or a compaction; a sub-agent's from its second request |
| rem.auto-mode.4 | `bypassPermissions` gets the same shell-first note, led by a line saying bypass-permissions mode is on instead of auto mode, under the same send-once rule (bundle `fvr`, `bypass`). | covered — `BYPASS_MODE_GUIDANCE` |
| rem.auto-mode.5 | Leaving auto mode for anything but plan mode (planning keeps auto mode in force), after an auto-mode note was sent: the next main-conversation request carries a `## Exited Auto Mode` note (auto mode is off; with the shell-first note, go back to the dedicated tools for reads, searches and edits). Nothing when auto mode is back on before that request; none in a sub-agent (bundle `pvr`). | covered — `AUTO_MODE_EXITED`, our words |
| rem.auto-mode.6 | No note at all for an agent without `Bash` and a writing tool (`Edit` or `Write`), such as `Explore` (the lean-model form only steers to the shell). | covered — `_shell_first` (`permissions/modes.py`) |

## mid-turn message

| id | rule | noeta today |
|---|---|---|
| rem.mid-turn.1 | Trigger: the user submits a message while a turn is running. Placement: a text block appended to the next tool-result user message, after the `tool_result` block(s); no `<system-reminder>` wrapper; no separate turn. | covered — `messages_to_wire` puts it in the tool results' turn |
| rem.mid-turn.2 | Content: a line saying the user sent a new message while the model was working; the message text; then an explanation that this is how mid-turn messages are surfaced (inside the running turn, often with the next tool result) and that the model should address it while continuing. | covered — `MID_TURN_MESSAGE`, our words |

## interrupt

| id | rule | noeta today |
|---|---|---|
| rem.interrupt.1 | When the user interrupts after a tool call completed, the `tool_use` and its real `tool_result` are kept and a text block `[Request interrupted by user]` (with a trailing newline) follows the result in the same user message; the user's next input is appended to that same message. | covered — `INTERRUPTED_MARKER` / `REJECTED_MARKER`; the next prompt joins the marker's turn (`messages_to_wire`) |
| rem.interrupt.2 | Observed in `m1` (unconfirmed mechanism): a prompt interrupted before any reply was kept (requests 0051 and 0054 are absent from the recording) shows up concatenated with the next prompt as one plain string, with no marker. | n/a — a recording artefact of Claude Code's input box (Escape before any output puts the prompt back; parity spec Phase 0 log), not a model-visible rule |

## instructions re-read (resume)

| id | rule | noeta today |
|---|---|---|
| rem.instructions-reread.1 | Trigger: `--continue`/resume when an instruction file changed since it was last sent. Placement: `<system-reminder>` block at the start of the first new user message, before the prompt. | deviation — `parity/deviations.md` Instruction files row: re-read with each new message, the note after it |
| rem.instructions-reread.2 | Content: instruction files were re-read when the session started; only files whose content changed are listed, and each listed text supersedes the version shown before; the earlier preamble (rem.instructions.2) still applies. Then per changed file the same path-and-kind line and full new text as rem.instructions.3. Unchanged files are not repeated. | covered — `REREAD_HEAD` and the changed files only (`_reread_changed`) |
| rem.instructions-reread.4 | noeta's form of the trigger, for durable tasks: the files shown so far (start and nested) and the auto-memory index (`MEMORY.md`, also when it is new: the bundle's re-read lists a file not shown before and the memory index is one of its files, `AutoMem`) are read again with every new message from the person, and the changed ones are shown again, in our words; the note follows the person's message in the same turn (`parity/deviations.md`). The digests of what was shown live in the task log. | deviation — the same Instruction files row; covered otherwise |
| rem.instructions-reread.3 | Observed defect, not to copy: the resumed prompt appears twice in that message (once with a trailing newline). | covered — not copied: the prompt is sent once |

## user answers through tools

| id | rule | noeta today |
|---|---|---|
| rem.answers.1 | `AskUserQuestion` answers come back as its `tool_result` text, not as a user message: each question quoted, `=`, the chosen label quoted, the chosen option's preview when it had one, and a closing line inviting the model to proceed using them. Details in `tools/AskUserQuestion.md`. | covered (`ask_user`; unanswered questions left out, none answered said so; no per-question notes: `deviations.md`) |
| rem.answers.2 | Approvals in default mode (`m2`) add no reminder; an approved call simply returns its normal result. | covered — an approved call returns its normal result, no note |

## at-mention (file attachments)

Not in the recordings; from the 2.1.312 bundle (`attachments` render map: `file`,
`directory`, `at_mention_reference`, `pdf_reference`; the reader that builds them).

| id | rule | noeta today |
|---|---|---|
| rem.at-mention.1 | Trigger: the person's prompt names a path with `@path` (also `@"path with spaces"` and `@path#L<start>-<end>` for those lines). Each existing file or directory becomes meta user content (`<system-reminder>` blocks) in the person's turn, ahead of the person's text; a path that does not exist adds nothing. | covered — `fs/mentions.py` (`@path`, `@"…"`, `#L<start>-<end>`), moved ahead of the person's text when the request is built |
| rem.at-mention.2 | A file is read through `Read` itself, so the file counts as read (a later `Edit` needs no new `Read`) and `Read`'s formats apply (image, notebook, PDF). Two blocks: `Called the Read tool with the following input: <the input as compact JSON>`, then `Result of calling the Read tool:` and the result text on the next line; an image result follows the first block as the image itself. | covered — read through `ReadTool`, the two blocks (`mentions.py`) |
| rem.at-mention.3 | A text file over `Read`'s limit is attached by its first 2,000 lines (the input carries that limit), followed by a note that it was cut, needs no mention, and the rest is there for `Read`. One too large even for that is named instead: the user @-mentioned it (with its size), it was not attached, read it in parts with offset and limit or search it, and only if every line is needed read it whole. | covered — `FIRST_LINES` 2,000 and the too-large note |
| rem.at-mention.4 | A directory is shown as a `Bash` call, `{"command": "ls <path>", "description": "Lists files in <path>"}`, and its result, the entry names one per line. | covered — `ls` through `Bash` (`mentions.py`) |
| rem.at-mention.5 | A PDF the model must read by pages is named instead (its page count and size): none of it is in the conversation; before anything else call `Read` with a few pages, read on a few pages a call, describe only pages read and say which ones an answer rests on. | covered — `PDF_REFERENCE` |
| rem.at-mention.6 | A path a deny rule keeps from `Read` is left out without a word. A path that could not be read is named: the user @-mentioned it, it was not attached, read it with the file tools before answering; without access to the files (a session that does not attach them) every mention is named the same way. (noeta: a path the model's `Read` would be asked about is named too: not attached, reading it needs the user's approval, which the model's own `Read` asks for.) | deviation — `parity/deviations.md` Attachments row: attached only when the model's `Read` would pass unasked |
| rem.at-mention.7 | A file the model has read already (that view of it) and that has not changed since sends nothing (bundle `already_read_file`, rendered empty). | covered — the attachment's `Read` runs as the model's (`by_model`), and its unchanged answer attaches nothing (`mentions.py`) |

## hook context

Not in the recordings (no hooks configured); from the 2.1.312 bundle (`hook_additional_context`,
`hook_blocking_error`).

| id | rule | noeta today |
|---|---|---|
| rem.hook-context.1 | Text a hook adds for the model is meta user content led by its hook name and a label: `additionalContext` by `<hook name> hook additional context: `, a `SessionStart` / `UserPromptSubmit` hook's plain stdout by `<hook name> hook success: ` (bundle `hook_success`, only those events); the hook name is the event (`UserPromptSubmit`, `SessionStart`, `SubagentStart`, `PostToolBatch`), or `<event>:<tool>` for a tool's events (`PreToolUse:Read`). A hook's blocking feedback (a tool's events, `Stop`, `SubagentStop`) is `<hook name> hook blocking error from command: "<command>": <reason>`, the command being the hook's command line, URL or prompt (bundle `hook_blocking_error`). | covered — `hooks/dispatch.py` `CONTEXT_LABEL` / `SUCCESS_LABEL` / `BLOCK_LABEL` |
| rem.hook-context.2 | `continue: false` is told to the model as `<hook name> hook stopped continuation: <reason>` (bundle `hook_stopped_continuation`): `PreToolUse` only when it gives a reason; `PostToolUse` with a default reason naming the event; `Stop` (named so in a sub-agent too) with its own default; `PostToolBatch` for a block as well, the turn ending, with the first reason given. | covered — `stopped_text` (`hooks/dispatch.py`); a tool hook's note is recorded once its turn has stopped |
| rem.hook-context.3 | A background hook's output reaches the model later: an `async` hook's JSON `systemMessage` and `additionalContext`, plain, with the next model call (bundle `async_hook_response`); an `asyncRewake` hook's exit code 2 wakes the model with `Stop hook blocking error from command "<hook name>": <stderr or stdout>`. | covered — `Dispatcher._in_background`: through the task's inbox (`hooks.async_response` signal, a `Say`), so any worker delivers it |
| rem.hook-context.4 | Hook text for the model (plain stdout, `additionalContext`, `systemMessage`) over 10,000 characters is saved to a file and replaced by the persisted-output preview (bundle `D_e`, `ggi`). | covered — `capped` (`hooks/dispatch.py`): the task's spill directory through its exec env; cut around an elision without one |

## Checked in the bundle (not recorded)

| id | rule | noeta today |
|---|---|---|
| rem.date-change.1 | When today's date differs from the last one the conversation was given, a meta note gives the new date and says not to announce it; checked before every model call (mid-turn included), sub-agents too. | covered (`environment` plugin) |
| rem.quiet-nudge.4 | The quiet nudge is for the main conversation only; sub-agents never get it. | covered (the plugin is only in `main.md`) |
| rem.file-changed.3 | Files checked: every entry of the read-file state (an LRU of 5,000 entries / 25 MiB), all at once, each compared by modification time first. | covered (`stat_many`, one batch) |
| rem.join.1 | Where two user messages are joined and a text block meets a text block, the earlier one gets a trailing newline; the opening context blocks (instructions, git status, attribution) are one message and get none. | covered (Anthropic adapter `_join`) |
| rem.git-status.6 | The repository is found by walking up from the working directory for `.git`; a status over 2,000 characters is cut there and followed by a note to run `git status` with the shell tool. | covered (`environment` plugin, note in noeta's words) |
| rem.shell-read.1 | A foreground `Bash` command that only shows files (no pipes or redirections; cat / nl / bat, head / tail, `sed -n 'N[,M]p'`, a lone grep that exited 0; joined by `&&` `;` `||` with echo / printf / true / `:` between) enters the read-file state for a file with no entry, at most 10 MB, read right after the command; the view must appear in the output unless it is grep or the output was cut. head / tail / sed record their lines (not watched; the same lines read again are answered as unchanged); `Edit` and `Write` take every such entry as read. | covered (`fs/shell_reads.py`) |
| rem.shell-read.2 | (bundle) A file a shell read showed counts as touched for nested and path-scoped instruction files, as a `Read` would; it does not trigger nested skill directories or `paths:` skills (only `Read` / `Edit` / `Write` do). | covered (the fs `read` record with `by: Bash`, read by the instructions plugin; a file a `Read` deny rule covers is recorded with `read_denied` and brings nothing) |
| rem.skill-handoff.1 | In auto mode a skill's or command's inline shell command that would be asked about is left to the model to run (its place says to run it first, exactly as written); a denied one still fails the skill. | deviation — the load is refused in every mode (`deviations.md`) |
| rem.ultrathink.1 | The word `ultrathink` in the person's prompt adds a meta note that they ask for deeper reasoning this turn (bundle `ultrathink_effort`); in the interactive terminal, on a model with effort below `xhigh`, the rest of that turn runs at `xhigh` (a prompt typed mid-turn raises the rest of it). | covered — `noeta.toolkit.ultrathink` (environment plugin); the effort only when a person follows the task (`interactive`) |
| rem.ultracode.1 | With `Workflow` offered, the word `ultracode` in the person's own prompt adds a meta note opting that turn into orchestration through `Workflow` (bundle `workflow_keyword_request`, setting `workflowKeywordTriggerEnabled`, default on). | covered — `workflow/opt_in.py` |
| rem.ultracode.2 | The session's `ultracode` setting (`--settings`, `--effort ultracode`): at a turn start while on, a full note when none is in the conversation since the last off note or compaction, a short one when 10 prompts passed since the last; when turned off after a note, an off note (bundle `ultra_effort_enter` / `ultra_effort_exit`); main conversation only. | covered — task config `ultracode` (changeable), `workflow/opt_in.py`; set through the config, not an effort value |
| rem.usd-budget.1 | With `--max-budget-usd`, every model call gets a system note `USD budget: $<used>/$<total>; $<remaining> remaining` (the session's cost so far, numbers as JavaScript prints them; bundle `budget_usd`). | covered — task config `max_budget_usd` (`noeta.toolkit.usd_budget`): the root task's and every task below it's cost, read from their logs |
| rem.agent-mention.1 | `@agent-<type>` (or `@"<type> (agent)"`) at the start of the prompt or after a space, for an agent type the session can start, adds a meta note that the user wants that agent invoked with the context it needs (bundle `agent_mention`). | covered — `subtasks/mentions.py`, with the first call that sees the prompt |
| rem.critical.1 | An agent definition's `criticalSystemReminder_EXPERIMENTAL` (`--agents` / SDK) is sent verbatim as meta content with every model call of that agent (bundle `critical_system_reminder`). | covered — `--agents` field → `environment` plugin `critical_reminder` |
| rem.mcp-dropped.1 | MCP tools whose input schema the Anthropic API would reject are left out when the server's tools are listed, and the model gets a `# Unavailable MCP Tools` note naming each (`"<tool>" (MCP server "<server>"): "<reason>"`, a count per server past 30) with what to say if asked (bundle `mcp_dropped_tools_delta`). | deviation — no schema check (`deviations.md` Tools) |

## s1 only

| id | rule | noeta today |
|---|---|---|
| rem.file-changed.1 | **s1 only.** Trigger: a file the model has in context (here `CLAUDE.md`, changed by a Bash command) changed on disk. Placement: `role: "system"` message after the tool-result message of the step that changed it. | covered (`fs` plugin: every remembered whole read, up to 5,000, checked in one `stat_many` batch per model call) |
| rem.file-changed.2 | Content: the path changed on disk since last read; the change is usually deliberate, so treat it as the current state and do not revert; if it looks wrong say so instead of undoing it; otherwise no need to mention it; then the relevant lines with line numbers (`N<TAB>text`), or, when they cannot be shown, that they are not shown and `Read` gives the current content (2.1.312 bundle, `edited_text_file`). The lines: each change with 8 unchanged lines either side, one block after another; past 8,192 characters cut at a line end and followed by `... [N lines truncated] ...` (bundle `oKs`). No note for a file last read with an offset or a limit. | deviation — `parity/deviations.md` Reminders row: lines shown only while the last whole `Read` is in the conversation, numbered as the file is now; 8 context lines and the 8,192-character cut covered (`fs/changes.py`) |
| rem.agent-list.1 | **s1 only.** A `general-purpose` sub-agent whose first environment message lacked the agent list received the "available agent types" list plus the auto-mode guidance as a `role: "system"` message after its first tool result. | covered — a sub-agent's second request carries the agent list and the auto-mode note (`subtasks` `reminders`) |

## Not observed in 2.1.312 baseline

| id | rule | noeta today |
|---|---|---|
| rem.absent.1 | No todo/task-list nudge, no `TodoWrite`/`TaskCreate` reminder (neither tool is in the baseline tool set). | covered — noeta has no todo tool and no todo nudge |
| rem.absent.2 | No MCP server-instructions block (the fixture server declares none), no deferred-tool delta reminder (the deferred set never changed mid-conversation; see rem.deltas), no diagnostics, no token-usage or context-window warning, no date-change reminder outside the environment message and compaction. | covered — the date-change note is checked before every model call (`environment`); no diagnostics or context-window warning, MCP instructions and deferred-tool notes only on a change |
