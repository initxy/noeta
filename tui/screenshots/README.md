# Screenshots

What noeta's terminal UI looks like, next to Claude Code 2.1.318's (observed in tmux on
2026-11-04). `make tui-screenshots` rewrites them from `tests/test_screenshots.py`, offline,
with a scripted model.

| File | Shows | Claude Code's counterpart |
|---|---|---|
| `trust.svg` | the first start in a folder not yet trusted: `Accessing workspace:`, the safety check, `❯ 1. No, exit` / `2. Yes, I trust this folder`, `Enter to confirm · Esc to cancel` | the same question and choices; Claude Code also links its security guide |
| `welcome.svg` | the welcome box, `◐ medium · /effort` over the prompt, the prompt between two rules with `❯` and the `Try "…"` placeholder, the shortcuts panel (`?`), the mode line `⏵⏵ accept edits on (shift+tab to cycle)` | the same; Claude Code's box shows its logo, model and account instead of a version |
| `transcript.svg` | `❯` prompt, `●` replies, `● Bash(pytest -q)` with `⎿` and `… +9 lines (ctrl+o to expand)`, two reads in a row as `Read 2 files`, `● Update(calc.py)` with `Added 1 line, removed 1 line` and a numbered red/green diff, `✻ Worked for 0s` | the same shapes; Claude Code also folds searches and listings into that line (`Read 1 file, listed 1 directory`) |
| `working.svg` | the spinner `✶ Fixing add()… (0s · ↓ 15 tokens · esc to interrupt)` using the in-progress task's `activeForm`, the task list (`✔` `◼` `◻`), a queued message under it | the same; Claude Code's spinner verbs are its own whimsical list |
| `approval.svg` | an edit's approval dialog: title, the call, the numbered diff it would make, `Do you want to proceed?`, `❯ 1. Yes`, the switch to accept edits, `No (esc)`, `Esc to cancel` | the same layout; Claude Code asks `Do you want to make this edit to calc.py?` and offers `Tab to amend` |
| `context.svg` | `/context`: the 10x10 `⛁`/`⛶` grid with the categories beside it | the same grid; Claude Code also lists memory files, skills and the autocompact buffer |
| `slash-menu.svg` | the slash-command menu under the prompt, filtered as you type, with descriptions | the same |

Not shown: Ctrl+R lists the earlier prompts of this run under the prompt, filtered as you
type (Claude Code's searches every session's). Claude Code's footer also counts down `N% until
auto-compact` when the context runs low; noeta's shows the share of the window in use, since
the compaction plugin does not publish its threshold.
