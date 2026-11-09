---
status: active
created: 2026-11-09
branch: tui/polish
---

# Terminal UI: copying, fast resume, folded tool runs, effort and project in the footer

## Goal

Five complaints from the owner about the terminal UI (2026-11-09), each fixed:

1. Text in the transcript cannot be copied.
2. Reopening a task (`--resume`, `-c`, `/resume`) draws it event by event: 12.5 s for a
   424-event task.
3. Tool calls read unlike Claude Code's: a run of calls should fold into one counted line
   (`Searched for 2 patterns, read 3 files, ran 4 shell commands`) that a click opens.
4. Whether `/effort` applies is not visible: the footer hides the effort for a model the
   built-in catalog does not list as a reasoning model, even when one is set.
5. The footer's directory (`…/Documents/noeta`) says little; the owner wants the project
   name and git branch, as Claude Code's status-line plugins (claude-hud) show them.

## Non-goals

- Claude Code's `statusLine` command setting: the owner ruled it out on 2026-10-29 (left to
  plugins); item 5 is a noeta plugin.
- Folding edits (`Edited 2 files (+12 -3)`): an edit keeps its diff on screen.
- Virtualised rendering of very long transcripts (only what resume needs).

## Context

- Textual 8.2.8 already selects text with the mouse; `Screen`'s `ctrl+c` copies it, but the
  app's own priority `ctrl+c` (clear input / quit) shadows it, and `Widget.get_selection`
  only reads `Text`/`Content`, so every `render.Hanging` (tool calls, results) is unselectable.
- Resume profile: each recorded event mounts its widgets and refreshes the footer; Textual
  restyles and recomposes the screen between events (O(n²)), and `update_status`/`_tick` walk
  the DOM with `query()`. Building the same widgets first and mounting them once took 1.2 s.
- Claude Code 2.1.319's collapsed group (its `collapsed_read_search`): verbs past tense when
  done, present + `…` while running; counts bold; parts joined by `, `: `searched for N
  patterns`, `read N files`, `listed N directories`, `ran N shell commands`, `called N tools`;
  `(ctrl+o to expand)`. `/copy` copies the last reply (`/copy N`: the Nth latest).
- Effort reaches the wire: the Anthropic adapter sends `output_config.effort` whenever the task
  has one; plugrelay maps it to the upstream's `reasoning_effort`. With none set nothing is sent.
- Plugins show status items through `Extension.ui` (`noeta.core.extension.StatusItem`); the
  terminal UI draws them in `#plugin-status` under the footer; web and desktop show them too.

## Decisions

- Copy: a mouse selection is copied when the button is released (and again by `ctrl+c` while
  it is there, which then neither clears the input nor counts toward quitting); every
  transcript widget is selectable. The clipboard is reached by OSC 52, plus `tmux load-buffer
  -w -` inside tmux and the local clipboard program (`pbcopy`, `wl-copy`, `xclip`, `xsel`)
  when not over ssh. `/copy [N]` as Claude Code's.
- Resume: what the log holds when a task opens is rendered off-screen and mounted at once; the
  follower continues after its last record. Live rendering keeps its per-event path.
- Folding: consecutive calls of the quiet kinds (reads, searches, listings, shell commands,
  web fetches/searches, other tools such as MCP's) with no reply text between them are one
  group with Claude Code's line; a single call shows as itself. Edits, writes, agents,
  workflows, todos and questions stay on their own. A click on a group's line, or on a call's
  folded result, opens or closes it; `ctrl+o` still opens everything.
- Paths in call headers are relative to the task's directory when under it (Claude Code's).
- Effort: the footer shows the task's effort whenever one is set, else the model's default for
  a catalogued reasoning model, else nothing; `/status` and the `/effort` picker say when none
  is sent.
- Project and branch: a shipped plugin (on by default, switchable) returns status items for the
  task's directory — the project (the git checkout's top folder, else the directory's name)
  and the branch with a dirty mark and ahead/behind; git runs hardened (no repo-configured
  programs, no transport: a partial clone's lazy fetch would run the repository's ssh command
  or `ext::` URL), cached briefly, with a timeout. The footer stops showing the directory.

## Plan

- [x] Copy: selectable transcript, copy on release and `ctrl+c`, clipboard fallbacks, `/copy [N]`, shortcuts text
- [x] Folding: group widget with Claude Code's line, click to open, relative paths in headers
- [x] Resume: batch render of the existing log, follower after it; footer refresh without DOM walks
- [x] Effort shown whenever set; `/status` and `/effort` say what is sent
- [x] Project/branch plugin, footer without the directory
- [x] Docs (`docs/guides/terminal.md` + `docs/zh/`), CHANGELOG, `tui/screenshots` rewritten

## Acceptance criteria

- Dragging over any transcript text (a reply, a tool call's line or result) puts that text on the
  clipboard and the footer says so; `ctrl+c` with a selection does not start the quit countdown.
- The 424-event task of the profile (the owner's, on the owner's server) opens in under 2 s
  headless (was 12.5 s), looking the same
  as when it was followed live; a pilot test pins batch rendering.
- Three reads, two greps and two shell commands in a row show as one line
  `Searched for 2 patterns, read 3 files, ran 2 shell commands (ctrl+o to expand)`; a click
  opens it; a failed call marks the line red.
- `/effort high` on a model outside the catalog shows `high` in the footer.
- The footer shows `<project> · ⎇ <branch>` for a task in a git checkout (and the project name
  alone outside one); switching the plugin off removes it.
- `make check` green; `make tui` green with coverage ≥ 85.

## Risks

- OSC 52 does not reach the clipboard in macOS Terminal.app or a tmux with `set-clipboard off`;
  the local program covers the non-ssh case only.
- Status items come only once the screen has a task (after the first message on a new screen).
- Folding changes what tests read off the screen (`Read 2 files`).

## Progress log

2026-11-09 Profiled resume (12.5 s, Textual restyle/compose per event); batch-mount prototype 1.2 s.
2026-11-09 Three subagents in worktrees (copy / folding+resume / effort+plugin), merged onto tui/polish; conflicts in the shortcuts text, `update_status` docstring and one test.
2026-11-09 Half the old resume time was `.assistant-message > *:last-child` (every mount restyled its siblings); narrowed to `MarkdownBlock:last-child`.
2026-11-09 Resume of the 424-event task: 1.2–1.9 s headless. Added on integration: replayed `Worked for` from record times, transcript anchored after the batch (a wrapping warning hid its last line), bullet on folded lines, a double click does not close what its first click opened.
2026-11-09 Moved git's read-only flags and filter check from `plugins/environment/git.py` to `toolkit/shell.py` for `project_status` (structure: its own commit).
2026-11-09 Review (fresh agent): P1 — a partial clone's lazy fetch ran the repository's `core.sshCommand` / `ext::` URL from `git status` (also on main in the environment plugin); fixed for both by blocking every transport in `GIT_READ_FLAGS` plus `GIT_READ_ENV`, with a test on such a repository. P3 — live output during the log read could land above the history; the batch now opens before the read. P3 — a synthetic 460-event task (38 turns of Read + 2 Bash, lists and code) opens in 3.2–4.0 s here; on main it did not finish.
