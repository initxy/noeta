---
status: proposed
created: 2026-10-29
---

# Open-source release: follow-ups and owner sign-offs

## Goal

Carry what the open-source release (`docs/specs/archive/2026-10-28-open-source-release.md`)
left open: the owner's sign-offs before any push or release, the follow-ups found by its
reviews, and the items decided not to do, with their reasons, so none is lost when that spec
is archived.

## Non-goals

Anything already shipped on `os-structure`; new features.

## Context

The release spec merged into local main after its final review found no P0/P1. Every item
below was checked against the code when it was written down; each "Follow-up spec" item gets
its own spec before work starts.

## Decisions

- 2026-11-03 (owner) `bypassPermissions` gets the shell-first note again, as in Claude Code. The owner asked why bypass mode was treated specially; the deviation had come from an eval that runs only in bypass mode, its first cause (a deny rule the shell could get past) was fixed generally, and the later side-by-side numbers were too thin to rest a difference from Claude Code on. pi has no permission modes; dsh was not checked for a mode-dependent note.
- D1 (2026-11-03, owner) A `Write`, `Edit` or `NotebookEdit` outside the working and additional directories is a permission question, as in Claude Code 2.1.312, not a refusal: `default`, `acceptEdits` and plan mode ask (offering `Edit(//<dir>/**)` as the "always" rule), `bypassPermissions` lets it through, `auto` sends it to the classifier, `dontAsk` refuses; deny rules, ask rules and protected paths apply as before, and an allow rule lets it through. The fs plugin's own fence is opt-in, `[plugins.fs] block_writes_outside = true` (off by default): such a write is refused in every mode before anyone is asked, the additional, plan and memory directories still open (Claude Code 2.1.314 added the same switch as `permissions.blockWritesOutsideWorkingDirectories`; it stays in the fs plugin so a host without the permissions plugin can fence too). A write through a symlink is judged by where it lands as well as by its name (allow rules included). A checkpoint restore puts back a snapshotted file outside the workspace only when an editing call wrote it without an error.

## Plan

### Owner sign-offs (before any push or release)
- [x] The committed Claude Code interface data (scripts/parity/README.md) is published with the repository (owner, 2026-10-29).
- [x] Private company, product and gateway names are out of every committed file (owner, 2026-10-29: they are private and are not published).
- [ ] Push `main` and publish: the owner signs off (no push, no release, no host data touched until then).
- [ ] A skill's `allowed-tools` plus `acceptEdits` writing `.noeta/skills` widens a trusted project's permissions, as in Claude Code. Owner decision (likely moot: `.noeta` is a protected path, so `acceptEdits` asks before writing `.noeta/skills`).
- [x] The recording file that could rebuild Claude Code's texts is gone from the unpushed history too (owner, 2026-10-29); that history no longer names private companies, products or gateways either, apart from files already public, byte for byte, in the pushed history.
- [x] The old `holds` table is dropped when a store is opened, after its trees are told the hold is gone (owner, 2026-10-29: old data may be deleted).
- [x] `noeta plugin add` installs no pip dependencies (it would run a package's build code; every worker would need them anyway); a plugin missing one names the install line (owner, 2026-10-29).
- [x] Tenant directories are per-machine copies read at worker start (D47). Moot: tenants are removed (owner, 2026-10-29; ADR-028).
- [ ] The README's `git+https` install line is untested until the branch is pushed, and its relative links will not resolve on a PyPI page. Owner decision, with the push and the first release.

- [ ] The 14 owner questions left by the context audit closure (`docs/specs/archive/2026-11-02-context-audit-closure.md`, "Open questions (owner)"): shell rc snapshot, bundled skills, `run_skill_script`, AskUserQuestion notes and skips, MCP prompt images, WebFetch preapproved hosts, ACP interactivity, a missing permission prompt tool, auto-mode inline skill commands, bare plugin agent names, the 64 KiB MCP cap, the one-piece fallback retry, headless keys and `client_hooks` on a clean `-p` exit, fs state replay cost. Each keeps the conservative choice written in that spec until the owner decides.

- [ ] The 11 open owner questions left by the context re-audit closure (`docs/specs/archive/2026-11-02-context-reaudit-closure.md`, "Open questions (owner)"): older Claude models' full prompt, `outputStyle`, dropping MCP tools with schemas the API rejects, WebFetch preapproved hosts, skill frontmatter `hooks:`, `skillOverrides`, an end-of-run review for agents that do not hand back, asking before `EnterPlanMode`, `--effort ultracode` and `--settings`, a 1M window option for Sonnet 4.6, the not-loaded hint for OpenAI short names. Each keeps the recommended choice written there until the owner decides. The twelfth, the shell-first note in `bypassPermissions`, was decided on 2026-11-03 (see Decisions).

- [ ] The 4 owner questions left by the context final closure (`docs/specs/archive/2026-11-02-context-final-closure.md`, "Open questions (owner)"): a withheld instruction file is reported to the person only (as the bundle renders it), `.git/info/exclude` for `settings.local.json`, `null` for an optional argument passes the input check, Haiku 4.5 thinks with a 31,999-token budget by default. Each keeps the choice written there until the owner decides.

- [ ] The 4 owner questions left by the task tools (`docs/specs/archive/2026-11-02-task-tools.md`, "Owner questions"): record Claude Code once with an older model that loads the task tools to check the reconstructed blank lines, `TaskCreated`/`TaskCompleted` hooks, clearing a finished list, `all_models` versus making `enabled = true` reach plugins. Each keeps the recommended choice written there until the owner decides.
- [ ] `quiet_nudge` finds its earlier nudges by their overridable text (as the task reminder did before 7939f2a7); find them by their record. Follow-up spec.

- [ ] The owner questions left by the plugin splits (`docs/specs/archive/2026-11-03-plugin-splits.md`): with the permissions plugin off, plan mode asks about every shell command (telling read-only commands apart lives in the permissions plugin; moving it to `noeta/toolkit` is a separate change); a host's `can_use_tool` no longer answers the repetition guard's ask; a mode switch sent in the same input batch right after `/plan` is applied first. Each keeps the current behaviour until the owner decides.

### Follow-ups
- [ ] `core/runner/task.py` is back to 730 lines (S29 took it to 614). Follow-up spec: another branch is changing the runner now, and a structural split under it would collide.
- [ ] The memory plugin's `git rev-parse --git-common-dir` (`noeta/plugins/memory/extension.py`, `_workspace`) runs without `GIT_NO_HOOKS` (D97). Follow-up spec: another branch is changing the memory plugin.
- [ ] A background command's planted `.git/commondir` lives until the command ends (D96). Follow-up spec.
- [ ] The macOS Seatbelt profile still allows `mach-lookup` and Apple Events. Follow-up spec: needs a macOS machine to test.
- [ ] MCP (`plugins/mcp/stdio.py`, `plugins/mcp/pool.py`) and the hooks runner (`plugins/hooks/runners.py`) still start processes with asyncio subprocesses: before CPython 3.14 a loop shut down mid-start waits forever (`LocalExecEnv` moved to `subprocess.Popen` for this). Follow-up spec.
- [ ] Holds under unknown commits: a hold whose last of three attempts commits and still raises `CommitUnknown` is reaped as lost; lease renewal does not retry on it; a second failed unhold ends in a false `hold_lost`. Follow-up spec.
- [ ] A parked MCP elicitation learns of a lost hold only when answered ("outcome unknown"). Follow-up spec.
- [ ] A crash between taking `hold_lost` and enqueuing an ended sub-agent's loss notice loses that notice. Follow-up spec.
- [ ] A sub-agent that takes its input, gets `hold_lost` and ends, while the root took its own copy when the sub-agent was live, is told by neither (one inbox read to the end append). Follow-up spec.
- [ ] Snapshots hold the whole conversation (a 20k-event log's is 12 MiB), so Postgres claims are bound by transfer. Follow-up spec.
- [ ] Deltas published before a front end's `LISTEN` is up are not replayed (the recorded reply still arrives). Follow-up spec.
- [ ] A link swapped in between the sandbox plan's resolution and bwrap's mounts is not caught (D46 of the release spec). Follow-up spec.
- [ ] `EnterWorktree`'s worktree is read-only in the sandbox (`.noeta`; `parity/deviations.md`). Follow-up spec: a task's working directory would have to change mid-task.
- [ ] Dead workers' job log directories (`w<pid>`) in an exec env that is not this machine are not swept (`plugins/shell/job_logs.py`). Follow-up spec.
- [x] Claude Code plugin skills and agents are named `<plugin>:<name>` (0ab0f24f).
- [x] `${CLAUDE_PLUGIN_ROOT}` in Claude Code plugin skill bodies (spec 2026-11-02-context-reaudit-closure, S13).
- [ ] A Claude Code plugin's MCP servers are replaced by an ACP editor's servers. Follow-up spec.
- [ ] A task that continues a conversation (`history_from`) does not save its carried messages' pasted images again: their paths are the source task's, which stop working once the source's image directory is cleaned (spec 2026-11-02-context-reaudit-closure, review T-P3-2). Copy them on continuation, or keep the source's directory while a continuation lives. Follow-up spec.
- [ ] `Runtime._running` is a set, not a count: a task reclaimed by another slot while its previous run is still finishing is dropped from it by that run's `finally`, and a later `CommitUnknown` claim could then adopt it again at the same epoch (two unlikely events together). Count runs per task, or compare claim objects. Follow-up spec.
- [ ] A redelivered channel message that arrives after its task ended is handled again in the continuation task (a one-shot agent answers twice). Follow-up spec.
- [ ] Workspace trust per task: an isolated sub-agent's worktree is not trusted, so "always" answers there are not saved to the project; `project_trusted` reads files on the event loop. Follow-up spec.
- [ ] `jobs.py`: empty job logs are never pruned by the byte cap, and a log writer that outlives its wait can recreate a log after cleanup. Follow-up spec.
- [ ] Channel continuation ids use a `.N` suffix, which can collide with a thread id that itself ends in `.<digits>`; every continuation copies its history (`history_from`). Follow-up spec.
- [ ] Memory: voiding a recall already compacted away finds "the first memory message after the prompt", which could be a later, valid recall. Follow-up spec.
- [ ] With memory listed before hooks, the memory recall selector still sees a blocked prompt's text (D104: the main model does not, and the recall is voided after the erase). Run the selector after the prompt hooks whatever the plugin order. Follow-up spec.
- [x] When Claude Code 2.1.312 joins text blocks into one user message, the text block before the joined one ends with `"\n"`; noeta now sends it (b3dfa6c1).
- [ ] `tests/plugins/test_hooks_events.py` sometimes (2 of about 14 runs) warns `PytestUnraisableExceptionWarning` from a garbage-collected asyncio subprocess transport; likely the same cause as the hooks runner's asyncio subprocesses above. Follow-up spec, with that item.
- [ ] The `delegate_parallel_review` eval scenario runs into its 240 s trial timeout in every trial (it still scores); raise the timeout or shorten the scenario. Follow-up spec.

- [ ] Pasted-image save: a file cut short by a crash mid-write is never written again (the script skips an existing file); write to a temporary name and rename. From the best-of-three A/B review.
- [ ] Recall selector: the retry without `thinking` follows any HTTP 400 and then remembers the model as refusing it for good; retry only on the thinking refusal. The once-per-reason WARNING keys on the error text, which may hold a request id. A loose (non-JSON) reply picks any listed file it names, even one it calls irrelevant.
- [ ] Git snapshot refresh: a turn woken by a timer does not record the HEAD at its end; the end-of-turn record has unit tests only, none through the runtime.
- [ ] The memory recall selector fails on gateways that refuse `thinking: disabled` or ignore `output_schema` (found with DeepSeek V4.1); now retried and warned, but recall stays off by default as in Claude Code.
- [ ] `delegate_parallel_review` scores 0.33 on DeepSeek V4.1 (7 of 9 trials hit the 600 s cap; the model reads 24 modules itself rather than delegating); `hook_block_adapts` 0.52 (the model probes for the hook after a block). Model behaviour under Claude Code's own wording; watch on the next model.
- [ ] Run the read-only `Bash` commands of one reply together, as Claude Code does (needs a per-input read-only flag on tools; the command classifier lives in the permissions plugin). About 1% of benchmark wall time. From the bench findings.
- [x] `-p`: no `AskUserQuestion`, no title side request, the `per-turn-control` beta only for models with per-turn effort, as in Claude Code (470ed2d9).

### Decided not to do (kept for the record)
- [x] ACP over `--server` cannot pass the editor's MCP servers. Won't do: they hold the editor's credentials and stay in its process.
- [x] MCP tools are never retry-safe on recovery. Won't do: a server's `idempotentHint` is not trusted.
- [x] A hold is not checked again on renewal (D42). Won't do: a child named onto another queue meanwhile is the host's explicit route, and refusing would stop a running background job.
- [x] Texts the recordings never show (deferred tools, MCP sign-in) are outside the own-words report. Won't do: the report is advisory (D39).
- [x] A short PDF stays page images. Won't do: a logged document block would be lost after a `/model` switch to a provider without one (`parity/deviations.md`).

## Acceptance criteria

Each item above is done, moved to its own spec, or dropped with the owner's agreement.

## Risks

## Progress log
- 2026-10-29 Created from the open-source release spec's open Follow-ups, its Phase 6 owner sign-off items and the final confirmation review's P2/P3 notes (`e947884a..9d6030f6`).
- 2026-10-29 Gates rerun on local main `59a9dc9e`: Python 3.11 / 3.12 / 3.13 each 2616 passed against Postgres 16, examples 4 + 9; `make parity` 0 mismatches (11 accepted deviations); `make cluster` 13/13; `make e2e` 7 passed; `make eval --trials 9` 1.000 against the 0.987 baseline (local, git-ignored `evals/results/2026-10-29T155843Z.json`; an earlier 9-trial run scored 0.979, its one zero an HTTP 429 from the gateway); the README's five-minute start in a clean HOME against a real model (`-p`, `-c -p`, stream-json). A review of the ledger (`9d6030f6..59a9dc9e`) found two open items missing from it: the recall selector seeing a blocked prompt (D104) and a hooks-test warning; both added above.
- 2026-10-29 Owner sign-offs: private names out of the tree, the old `holds` table told and dropped on open, and main's unpushed history rewritten (`git filter-repo` on `origin/main..main`: the recording file removed, private names replaced in files and messages); the tip's tree is unchanged.
- 2026-10-29 Owner: tenants stay (off unless configured); the parity data is published; plugins install no dependencies, a missing one names its install line.
- 2026-10-29 Owner: the whole of `main`'s history is rewritten locally as well (private names, local paths and the recording file out of every commit and message; the tip's tree unchanged), so it no longer shares history with the pushed `main`: publishing it is a force push. The pushed release tags and GitHub's pull-request refs still reach the old commits; removing those is part of the push sign-off.
- 2026-10-29 Owner reversed "tenants stay": tenants are removed entirely (ADR-028; `docs/specs/archive/2026-10-29-drop-tenants.md`). The tenant-only items above are moot and dropped.
- 2026-11-03 Gates on local main `a9daa2cf`: `make check` 3515 passed (90% coverage), `make parity` 0 mismatches (11 accepted deviations), `make cluster` 14 passed, `make e2e` 7 passed. `make eval --trials 9` (35 scenarios, DeepSeek V4.1): 0.905 against the 0.953 baseline, "no regression"; every drop is a trial timeout (18), and every trial that finished scored as the baseline. A rerun of the ten affected scenarios matched; a paired run of the plan-mode scenarios at `49712232` (before the plan-mode split) and `a9daa2cf`, side by side, timed out alike (12 each; 0.596 vs 0.607): gateway load, not the split.
- 2026-11-03 R5 (no shell-first note in `bypassPermissions`) measured again now that deny rules hold against the shell: with the note 0.831, without it 0.930 over the five tool-choice and deny-rule scenarios (9 trials each, side by side; finished trials only: `read_over_shell_cat` 0.67 / 0.94, `edit_not_write` 0.86 / 0.97, `deny_rule_respected` 0.88 / 1.00). The deny-rule gap is down to one trial; the tool-choice scenarios score the dedicated tools, so the numbers confirm rather than prove. `parity/deviations.md` rests the row on the main prompt's own rule; the owner question stays open.
- 2026-11-03 Eval weak spots found by an independent overfitting check (no fixture or scenario text in `noeta/`, no model-specific code): `todo_list_multi_step` and `todo_reminder_long_run` never move (DeepSeek does not load the task tools); a scenario may drop 0.34 before the gate flags it; every trial runs in bypass mode; the injection scorer's half credit cites a main-prompt rule 13 that no longer exists. Follow-ups, not fixed.
- 2026-11-03 R5 closed: the shell-first note is sent in `bypassPermissions` again (Claude Code's behaviour); the deviation row is gone, `rem.auto-mode.4` is covered, golden requests updated.
- 2026-11-03 D1 on branch `fs-write-fence`: the fs plugin no longer refuses writes outside the working directories unless `block_writes_outside` is set (then in `before_tool`, before any ask); the permissions plugin asks instead of refusing (`rule_check.py` `_edit`); the `parity/deviations.md` Permissions row is gone; checkpoint restores reach outside paths. The refusal text the model sees changed (only with the fence on), so the eval runs after the merge.
- 2026-11-03 Gates on `fs-write-fence` `7b4c65e0`: `make check` 3528 passed (89.67% coverage), `make parity` 0 mismatches (11 accepted deviations), `make cluster` 14 passed, `make e2e` 7 passed; `make eval` after the merge.
- 2026-11-03 Review of `fs-write-fence`: `_edit` judged only the path as named, so with the fs fence off a write through `<cwd>/link -> /outside` passed `acceptEdits` and `auto` as a write inside; now judged as named and where it lands (`real_scope`, as `_read`), the "always" rule and the prompt name the real directory, and an allow rule must match the real path. Rewind restores an outside file only when an editing call finished writing it (from the logs it already reads); with `block_writes_outside` such a call never runs, so no separate fence check.
- 2026-11-03 Re-review: with the working directory reached through a link, every path differs from its resolved form, so an allow rule written against the working directory as named (`Edit(/src/**)`) never matched and ordinary writes were asked about. An allow rule now counts when it matches the resolved path, or the named one while the resolved path stays in the working directories as resolved (`Read` and `Edit` alike), and a path naming a working directory by its resolved spelling is inside.
- 2026-11-04 Merged `cc-align-fixes`, `fs-write-fence` and `multi-provider` into local main (`ed42d0d4`), each reviewed independently and its findings fixed. Gates on main: `make check` 3586 passed (90% coverage), `make parity` 0 mismatches (11 accepted deviations), `make cluster` 14 passed, `make e2e` 7 passed. Eval on `8e6fb8aa` (before these merges): 0.831 over all trials with 50 timeouts; the 265 finished trials average 0.953, the baseline. Event timestamps of timed-out trials show noeta under 0.2 s per step and 7–158 s per model call: gateway latency. A low-concurrency rerun of the four slowest scenarios timed out as well (9 of 10) and was stopped.
