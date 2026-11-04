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

## Claude Code's web and desktop app features

| Feature | Here |
| --- | --- |
| Sessions sidebar: list, new, resume, grouped by project directory, status (running / needs you / waiting / ended) | done |
| Rename a session (title button, `/rename`) | done |
| Transcript: markdown, code highlighting, streamed text, collapsible thinking | done |
| Tool cards: Bash command and output (long output folded), Read/Grep/Glob compact, Edit/Write/MultiEdit as diffs, WebFetch, MCP tools, sub-agents nested | done |
| Compaction markers, retries, failures, interrupts | done |
| Approval cards: yes / always allow (the rule) / switch mode / no with feedback; sub-agents' too | done |
| AskUserQuestion form (single, multi, other text) | done |
| Plan review (ExitPlanMode's plan with its choices) | done |
| Composer: multiline, Enter to send, stop (button or Esc), queued messages | done |
| Slash-command menu from the server's commands | done |
| `@` file mentions completed from the session's directory | done |
| Image paste, drop and upload | done |
| Permission-mode picker (bypass only when the server allows it), model picker | done |
| Usage: tokens, cost, context fill | done |
| Rewind to a checkpoint | done |
| Todo list panel | done |
| Light and dark themes, phone-sized layout | done |
| Keyboard shortcuts on approval cards (1/2/3), shift+tab mode cycling | not yet |
| Cloud environments, repository picker, branches and pull requests (claude.ai/code) | not here: noeta runs in the server's directories |
| Diff review across a whole session, file tree, terminal pane (desktop) | not yet |
| Search across sessions, archive and delete | not yet: the API has no delete |
