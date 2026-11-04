# Screenshots

What noeta's terminal UI looks like, next to Claude Code 2.1.318's (observed in tmux on
2026-11-04). `make tui-screenshots` rewrites them from `tests/test_screenshots.py`, offline,
with a scripted model.

| File | Shows | Claude Code's counterpart |
|---|---|---|
| `welcome.svg` | the welcome box, the prompt between two rules with `❯`, the shortcuts panel (`?`), the mode line `⏵⏵ accept edits on (shift+tab to cycle)` | the same; Claude Code's box shows its logo, model and account instead of a version and task id |
| `transcript.svg` | `❯` prompt, `●` replies, `● Bash(pytest -q)` with `⎿` and `… +9 lines (ctrl+o to expand)`, `Read 3 lines`, `● Update(calc.py)` with `Added 1 line, removed 1 line` and a numbered red/green diff, `✻ Worked for 0s` | the same shapes; Claude Code also groups consecutive reads into one line (`Read 1 file, listed 1 directory`) |
| `working.svg` | the spinner `✶ Fixing add()… (0s · ↓ 15 tokens · esc to interrupt)` using the in-progress task's `activeForm`, the task list (`✔` `◼` `◻`), a queued message under it | the same; Claude Code's spinner verbs are its own whimsical list |
| `approval.svg` | the approval dialog: title, the call, `Do you want to proceed?`, `❯ 1. Yes`, the gate's "don't ask again" choice, `No (esc)`, `Esc to cancel` | the same layout; Claude Code shows the edit's diff inside the dialog and offers `Tab to amend` |
| `context.svg` | `/context`: the 10x10 `⛁`/`⛶` grid with the categories beside it | the same grid; Claude Code also lists memory files, skills and the autocompact buffer |
| `slash-menu.svg` | the slash-command menu under the prompt, filtered as you type, with descriptions | the same |
