# Screenshots

Taken by the end-to-end test (`npm run e2e`, `e2e/app.spec.ts`) against a real `noeta serve`
on the offline scripted provider, so they are rewritten on every run.

| File | Shows |
| --- | --- |
| `01-new-session.png` | a new session: mode and model pickers before the first prompt |
| `02-streamed-reply.png` | a streamed markdown reply with a highlighted code block, a collapsed thought, usage |
| `03-approval-card.png` | a Bash approval card with Claude Code's choices |
| `04-bash-card.png` | the Bash card with its command and output |
| `05-write-diff.png` | a Write as a diff, after "Yes, and accept edits" switched the mode |
| `06-streaming.png`, `07-interrupted.png` | text streaming, then stopped (what streamed stays) |
| `08-plan-review.png` | plan mode's plan review card |
| `09-todos.png`, `10-subagent.png` | the task list panel; a question answered; a sub-agent's nested transcript |
| `11-slash-menu.png` | the slash-command menu (the page's commands and the server's) |
| `12-rewind.png` | rewind to before a prompt (conversation, code or both) |
| `13-theme.png`, `14-phone.png`, `15-phone-sessions.png` | dark theme; a phone-sized screen and its sessions drawer |
| `16-search-archive.png` | the session search narrowing the list; a row's archive button |

## Claude Code's web and desktop app features

| Feature | Here |
| --- | --- |
| Sessions sidebar: list, new, resume, grouped by project directory, status (running / needs you / waiting / ended) | done |
| Rename a session (title button, `/rename`) | done |
| Transcript: markdown, code highlighting, streamed text, collapsible thinking | done |
| Tool cards: Bash command and output (long output folded), Read and the task tools compact, Edit/Write/MultiEdit as diffs, WebFetch, MCP tools, sub-agents nested (each built-in plugin's view is its web part, `web/src/plugins/<name>/`) | done |
| Compaction markers, retries, failures, interrupts | done |
| Approval cards: yes / always allow (the rule) / switch mode / no with feedback; sub-agents' too; Edit/Write cards show the diff | done |
| Keyboard: digits / arrows / Enter / Esc on approval cards, Shift+Tab cycles the mode, Enter runs the highlighted slash command | done |
| AskUserQuestion form (single, multi, other text) | done |
| Plan review (ExitPlanMode's plan with its choices) | done |
| Composer: multiline, Enter to send, stop (button or Esc), queued messages | done |
| Slash-command menu from the server's commands | done |
| `@` file mentions completed from the session's directory | done |
| Image paste, drop and upload | done |
| Permission-mode picker (bypass only when the server allows it), model picker | done |
| Usage: tokens, cost, context fill; within 20K tokens of the auto-compact threshold "N% until auto-compact" ("Context low … Run /compact" when auto is off), as the TUI | done |
| Rewind to a checkpoint (the prompt back in the composer, the rewound session replaced) | done |
| Continue an ended session (send to it: a new session carries its conversation on) | done |
| Links in a new tab; remote images in replies shown as links, not loaded; served with a CSP | done |
| Disconnect (forget the saved token) | done |
| Todo list panel | done |
| Light and dark themes, phone-sized layout | done |
| Cloud environments, repository picker, branches and pull requests (claude.ai/code) | not here: noeta runs in the server's directories |
| Diff review across a whole session, file tree, terminal pane (desktop) | not yet |
| Search sessions (titles, first prompts, folders of the listed sessions) | done |
| Archive a session, list the archived ones, bring one back | done (the `archive` signal; `GET /summaries?archived=`) |
| Delete a session | not here: the store keeps every task's log (continued and forked sessions read their source's); archive hides it instead |

Regenerate with `NOETA_SCREENSHOTS=1 make web`.
