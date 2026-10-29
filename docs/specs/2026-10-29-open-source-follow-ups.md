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

## Plan

### Owner sign-offs (before any push or release)
- [ ] Owner signs off on publishing the committed Claude Code interface data (scripts/parity/README.md) and the remaining owner decisions before any push.
- [ ] Owner decides on the internal company and host names in committed docs before any push (left as they are until then): the release spec's D9 ("gateway token gateway"); `docs/specs/2026-10-26-noeta-1-0-redesign.md` lines 45 (host-app, host-app-2, noeta-agent), 63, 370, 371, 385, 395, 418 and 420 (host-app); `docs/specs/archive/2026-10-27-layout-split.md` line 27 and `docs/specs/archive/2026-10-27-noeta-1-0-layout.md` line 19 (host-app); `docs/specs/archive/2026-10-28-claude-code-parity.md` line 365 (the `gateway/seed-evolving` upstream).
- [ ] Push `main` and publish: the owner signs off (no push, no release, no host data touched until then).
- [ ] A skill's `allowed-tools` plus `acceptEdits` writing `.noeta/skills` widens a trusted project's permissions, as in Claude Code. Owner decision (likely moot: `.noeta` is a protected path, so `acceptEdits` asks before writing `.noeta/skills`).
- [ ] The old `holds` table stays unused in upgraded stores. Owner decision (dropping it deletes data on a host's store).
- [ ] `noeta plugin add` installs no pip dependencies. Owner decision (it would run a package's build code on install).
- [ ] Tenant directories are per-machine copies read at worker start (D47; `docs/server.md`). Owner decision on a shared source.
- [ ] The README's `git+https` install line is untested until the branch is pushed, and its relative links will not resolve on a PyPI page. Owner decision, with the push and the first release.

### Follow-ups
- [ ] `core/runner/task.py` is back to 730 lines (S29 took it to 614). Follow-up spec: another branch is changing the runner now, and a structural split under it would collide.
- [ ] The memory plugin's `git rev-parse --git-common-dir` (`noeta/plugins/memory/extension.py`, `_workspace`) runs without `GIT_NO_HOOKS` (D97). Follow-up spec: another branch is changing the memory plugin.
- [ ] A background command's planted `.git/commondir` lives until the command ends (D96). Follow-up spec.
- [ ] The macOS Seatbelt profile still allows `mach-lookup` and Apple Events (D99 refuses tenants there instead). Follow-up spec: needs a macOS machine to test.
- [ ] MCP (`plugins/mcp/stdio.py`, `plugins/mcp/pool.py`) and the hooks runner (`plugins/hooks/runners.py`) still start processes with asyncio subprocesses: before CPython 3.14 a loop shut down mid-start waits forever (`LocalExecEnv` moved to `subprocess.Popen` for this). Follow-up spec.
- [ ] Holds under unknown commits: a hold whose last of three attempts commits and still raises `CommitUnknown` is reaped as lost; lease renewal does not retry on it; a second failed unhold ends in a false `hold_lost`. Follow-up spec.
- [ ] A parked MCP elicitation learns of a lost hold only when answered ("outcome unknown"). Follow-up spec.
- [ ] A crash between taking `hold_lost` and enqueuing an ended sub-agent's loss notice loses that notice. Follow-up spec.
- [ ] A sub-agent that takes its input, gets `hold_lost` and ends, while the root took its own copy when the sub-agent was live, is told by neither (one inbox read to the end append). Follow-up spec.
- [ ] Snapshots hold the whole conversation (a 20k-event log's is 12 MiB), so Postgres claims are bound by transfer. Follow-up spec.
- [ ] Deltas published before a front end's `LISTEN` is up are not replayed (the recorded reply still arrives). Follow-up spec.
- [ ] A worker's `TenantFence.servers()` reads its working directory when it checks, not at start. Follow-up spec.
- [ ] A link swapped in between the sandbox plan's resolution and bwrap's mounts is not caught (D46; file tools keep the opened-file fence). Follow-up spec.
- [ ] `EnterWorktree`'s worktree is read-only in the sandbox (`.noeta`; `parity/deviations.md`). Follow-up spec: a task's working directory would have to change mid-task.
- [ ] Dead workers' job log directories (`w<pid>`) in an exec env that is not this machine are not swept (`plugins/shell/job_logs.py`). Follow-up spec.
- [ ] Claude Code plugin directories: no `<plugin>:<name>` namespacing of skills and agents (agent names may not hold `:`), no `${CLAUDE_PLUGIN_ROOT}` in skill bodies, a plugin's MCP servers are replaced by an ACP editor's servers. Follow-up spec.
- [ ] `Runtime._running` is a set, not a count: a task reclaimed by another slot while its previous run is still finishing is dropped from it by that run's `finally`, and a later `CommitUnknown` claim could then adopt it again at the same epoch (two unlikely events together). Count runs per task, or compare claim objects. Follow-up spec.
- [ ] A redelivered channel message that arrives after its task ended is handled again in the continuation task (a one-shot agent answers twice). Follow-up spec.
- [ ] Workspace trust per task: an isolated sub-agent's worktree is not trusted, so "always" answers there are not saved to the project; `project_trusted` reads files on the event loop. Follow-up spec.
- [ ] `jobs.py`: empty job logs are never pruned by the byte cap, and a log writer that outlives its wait can recreate a log after cleanup. Follow-up spec.
- [ ] Channel continuation ids use a `.N` suffix, which can collide with a thread id that itself ends in `.<digits>`; every continuation copies its history (`history_from`). Follow-up spec.
- [ ] Memory: voiding a recall already compacted away finds "the first memory message after the prompt", which could be a later, valid recall. Follow-up spec.
- [ ] With memory listed before hooks, the memory recall selector still sees a blocked prompt's text (D104: the main model does not, and the recall is voided after the erase). Run the selector after the prompt hooks whatever the plugin order. Follow-up spec.
- [ ] `tests/plugins/test_hooks_events.py` sometimes (2 of about 14 runs) warns `PytestUnraisableExceptionWarning` from a garbage-collected asyncio subprocess transport; likely the same cause as the hooks runner's asyncio subprocesses above. Follow-up spec, with that item.
- [ ] The `delegate_parallel_review` eval scenario runs into its 240 s trial timeout in every trial (it still scores); raise the timeout or shorten the scenario. Follow-up spec.

### Decided not to do (kept for the record)
- [x] A budget may end over its limit by one call's cost (checked before each call, D52). Won't do: a call's cost is known only after it returns.
- [x] ACP over `--server` cannot pass the editor's MCP servers. Won't do: they hold the editor's credentials and stay in its process.
- [x] MCP tools are never retry-safe on recovery. Won't do: a server's `idempotentHint` is not trusted.
- [x] A hold is not checked again on renewal (D42). Won't do: a child named onto another queue meanwhile is the host's explicit route, and refusing would stop a running background job.
- [x] Operator hooks run as the OS user in a tenant's directory, and the sandbox plan's `git rev-parse --git-path hooks` reads the tenant's config; `find -L` and glob expansion list names under home directories; credentials under other names are readable unless `denyRead` names them. Won't do: documented under "Not a boundary" in `docs/server.md`.
- [x] Texts the recordings never show (deferred tools, MCP sign-in) are outside the own-words report. Won't do: the report is advisory (D39).
- [x] A short PDF stays page images. Won't do: a logged document block would be lost after a `/model` switch to a provider without one (`parity/deviations.md`).

## Acceptance criteria

Each item above is done, moved to its own spec, or dropped with the owner's agreement.

## Risks

## Progress log
- 2026-10-29 Created from the open-source release spec's open Follow-ups, its Phase 6 owner sign-off items and the final confirmation review's P2/P3 notes (`e947884a..9d6030f6`).
- 2026-10-29 Gates rerun on local main `59a9dc9e`: Python 3.11 / 3.12 / 3.13 each 2616 passed against Postgres 16, examples 4 + 9; `make parity` 0 mismatches (11 accepted deviations); `make cluster` 13/13; `make e2e` 7 passed; `make eval --trials 9` 1.000 against the 0.987 baseline (`evals/results/2026-10-29T155843Z.json`; an earlier 9-trial run scored 0.979, its one zero an HTTP 429 from the gateway); the README's five-minute start in a clean HOME against a real model (`-p`, `-c -p`, stream-json). A review of the ledger (`9d6030f6..59a9dc9e`) found two open items missing from it: the recall selector seeing a blocked prompt (D104) and a hooks-test warning; both added above.
