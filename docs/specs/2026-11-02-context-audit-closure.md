---
status: active
created: 2026-11-02
branch: ctx-audit-closure
---

# Context audit closure

## Goal

Every model-visible difference from Claude Code (CC) 2.1.312 that an independent audit of
local main @95dce942 found without a written reason, every row of `parity/deviations.md`'s
"Not yet matched" section, and every stale or wrong row of the rule catalogue
(`parity/rules/`) is closed: matched to CC, or kept as a deviation written here as
others (CC, pi, dsh) / noeta / why better, with numbers where they can be measured, and as a
row in `parity/deviations.md`. "Follow-up" alone is not a reason.

## Non-goals

- Features CC does not have, beyond what multi-machine operation or the extension mechanism
  needs. Uncertain cases go to the owner (Open questions), not into code.
- Pushing, releasing, touching another program's data, uploading anything.
- Differences the audit classed as justified with evidence (they keep their rows).

## Context

- CC baseline 2.1.312 (`parity/2.1.312/`); its bundle's strings were read for the facts below.
  pi: `badlogic/pi-mono`, dsh: `deepseek-ai/DeepSeek-Harness`, shallow clones read for the
  pi/dsh columns (same commits as `docs/specs/archive/2026-10-29-best-of-three.md`).
- Owner's rule for context: CC's way by default; a different way only when better than CC, pi
  and dsh, written as others / noeta / why, with evals or token counts where possible.
- Starting point: local main @95dce942, `make check` green; `make parity` 0 mismatches,
  11 accepted deviations.

## Items

Each item: the difference, the resolution (match / deviation / owner), and where it lands.
Ids are this spec's own.

### Environment and git snapshot
- **E1** A working directory below a repository's top says "Is a git repository: false" and
  sends no git snapshot (noeta looked for `<cwd>/.git` only). CC walks up for `.git`. Match.
- **E2** Git status cut at 2,048 bytes at a line boundary with an own "N of M paths" marker; CC
  cuts at 2,000 characters with a fixed note. Match (CC's cut; the note in noeta's words).
- **E3** Status "(not captured)" when the repository's own config defines a filter driver.
  Deviation (security); row added.
- **E4** The environment's "Shell:" line named the person's login shell (`$SHELL`), while
  `Bash` always runs `bash`. CC names the shell its commands run in. Match CC's meaning.
- **E5** `Bash` is not set up from the person's shell startup file (CC: a snapshot of the
  rc file's aliases, functions, options and PATH, sourced before each command, in the person's
  own shell). See Decisions; owner question.

### Reminders
- **R1** The changed-file check looks at the last 20 read files; CC at every file in its
  read-file state (an LRU of 5,000 entries). Match.
- **R2** Date-change note only at a turn's start. Check CC; match.
- **R3** No quiet nudge in sub-agents. Check CC; match.
- **R4** CC ends the text block before a joined one with `"\n"` (one byte). Fix or row.

### Skills and agents
- **S1** No bundled skills in the listing. Deviation; owner question for the generic ones.
- **S2** Claude Code plugin skills (and agents) not named `plugin:name`. Match.
- **S3** A skill whose `` !`cmd` `` would be asked about refuses to load in every mode; CC's auto
  mode hands it to the classifier. Check CC; match.
- **S4** A typed `/<name>` of a `context: fork` skill loads inline; CC forks. Match.
- **S5** Directory-scoped skill names (`apps/web:deploy`). Check what CC 2.1.312 builds.
- **S6** `run_skill_script` (noeta-only, opt-in, off by default). Owner question (already on
  the deletion list); row added meanwhile.
- **A1** Agent list lacks CC's `claude`, `claude-code-guide`, `statusline-setup`. Deviation.
- **M1** Sub-agents get no `MEMORY.md` index or memory section. Check CC; match.

### Tools
- **T1** MCP tool descriptions capped at 1,024 bytes; CC 4,096 characters plus
  `… [truncated]` (16,384 once loaded through tool search). Match.
- **T2** `-p` offers `AskUserQuestion`, sends the title side request and the per-turn-control
  beta for every model. CC does none of these in `-p` (the beta only for models with per-turn
  effort). Match.
- **T3** `--json-schema` goes to the provider as `output_schema`; CC offers a
  `StructuredOutput` tool whose input schema is the schema. Match.
- **T4** `AskUserQuestion` needs every question answered; CC accepts unanswered ones. Match.
- **T5** MCP result sizes are estimated; CC counts with the API's token counter between half
  and four times the limit. Decide (likely a row).
- **T6** No raw-markdown answer for CC's preapproved documentation hosts in `WebFetch`. Match.
- **T7** Non-UTF-8 files: `Read` replaces bad bytes, `Edit`/`Write` refuse. CC reads and writes
  UTF-16LE (byte-order mark) and keeps a UTF-8 byte-order mark; other non-text files get a note
  on `Read` and a refusal on `Edit`/`Write`. Match.
- **T8** Headless: SDK hook callbacks ignored; `--permission-prompt-tool` takes only `stdio`.
  Match or row.
- **T9** MCP prompts and resources entering unasked are capped at 64 KB. Check CC; match.

### Compaction
- **C1** The summary's transcript line is left out when the exec env is not local. Row.
- **C2** The "oldest part did not fit" note when the summarize call drops head rounds. Check
  CC; match or row.

### Found while closing (rule catalogue `noeta today` column and the agents' reports)
- **S7** Fork skills in the background by default (CC `background ?? true`): already so.
- **N1** A background notice taken mid-turn was a `<system-reminder>` in the tool-result turn; CC
  sends a `role: "system"` turn. Match.
- **N2/N3/N8** Side requests (title, plan name, digest): identity line, `thinking`, effort,
  `max_tokens`; digest order and rules; plan named on approval. Match.
- **N4** Sub-agent prompts lacked the `<total_tokens>` line. Match.
- **N5** The classifier for a sub-agent without instructions had no instructions block;
  `omitClaudeMd` agent files dropped managed files. Match.
- **N6** MCP over-budget cut: CC cuts at four characters per budget token. Row.
- **N7** MCP prompt commands kept only text blocks. Partly matched; row for images.
- **P1** After a compaction the stored git snapshot was re-sent; CC takes it again. Match.
- **P2** A broken stream: CC falls back to a non-streaming call (64,000, 300 s) on every call.
  Match (Anthropic).
- **P3** Files read with `cat` / `head` / `tail` / `sed -n` / `grep` were not restored after a
  compaction. Match.
- **P4** A re-attached skill lacked its source. Match.
- **Q1** Files a read-only `Bash` command showed did not count as read for `Edit` / `Write` and
  the changed-file note. Match.
- **Q2** CC notes after a `Bash` command that modified files the model had read. Match.
- **Q3** A shell read triggers nested instruction files (and path-scoped skills) in CC. Match.

### Rule catalogue
- **K1** Stale `tool.Skill.4/.5/.9`; wrong "covered" on `main.env.8`, `main.env.10`,
  `tool.mcp.6`; no noeta status on the reminders, compaction and sub-agent rule files.
- **K2** pi / dsh notes on `parity/deviations.md` rows that lack them.

## Decisions

Matched items need no entry: CC's way, with a test each (Progress log names the branches).
Kept differences, each also a `parity/deviations.md` row (the pi / dsh column there):

- **E3 status with a repository filter driver.** CC: runs `git status` with the repository's
  filters (off only in a linked worktree). pi, dsh: no git status for the model. noeta: `(not
  captured)` when the repository's own config defines a filter driver. Why: a `clean` program
  from the repository runs on `git status`, on a worker, before any permission check.
- **E5 no shell rc snapshot.** CC: snapshots the person's rc (`<shell> -c -l`; aliases, functions,
  options, login PATH) and runs commands in their shell. pi: `/bin/bash -c`, no rc. dsh: `bash -c`,
  no rc. noeta: `bash -c`, environment minus credentials. Why: per-machine state would make one
  task behave differently per worker; taking it runs arbitrary code. Measured (zsh with a plugin
  framework): snapshot 0.7-1.8 s, 643 KB, +40-60 ms per command; a terminal-started front end
  already has the rc's PATH (same 15 directories). Owner question 1.
- **T5 CJK-aware estimate.** CC: four characters a token. noeta: a CJK character one token. Why:
  CC's estimate puts 40,000 Chinese characters at 10,000 tokens, under half the 25,000 budget, so
  it is never counted and passes far over; arithmetic, no local tokenizer to measure.
- **N6 MCP over-budget cut by tokens.** CC: four characters per budget token. noeta: the token
  measure that judged it over. Why: CC's cut passes CJK text up to four times over the budget;
  ASCII cuts at the same place (100,000 characters).
- **Side requests on non-Anthropic models** keep reasoning off. CC calls only Anthropic models,
  where an absent `thinking` means none; on OpenAI-shaped endpoints absent means full reasoning.
- **Digest untrusted-page rule** added outside documentation sites (CC: only its untrusted
  variant). Why: unattended server tasks fetch arbitrary pages.
- **`Edit` keeps mixed line endings** (CC rewrites the whole file to its main ending). Why: the
  change on disk is only the edit.
- **Shell reads keep a digest, not the shown text** (CC keeps up to 4 KB). Why: the log is a
  worker's only state; the digest is what `Edit` / `Write` compare.
- **Fork skills always background** (CC foreground with `background: false` or `-p`). Why: a
  durable task does not hold a tool call open across a sub-agent that may move worker.
- **Interactivity is task config**, and an MCP permission-prompt tool does not enable
  `AskUserQuestion`. Why: tasks wake on any worker; the MCP tool answers permissions only.
- **Stream-json hook callbacks** fire only in the process holding the client; a missing MCP
  prompt tool refuses each call instead of exiting. Why: only that process can reach the client.
- **StructuredOutput validation** by noeta's own checker (no `format`, no strict form); cap via
  `max_retries`. Why: no new dependency; noeta reads no CC environment variables.
- **Note after a command rewrote read files** compares with the last time the task knew for
  the file, not the command's start on CC's clock. Why: an exec env's clock may differ from the
  worker's.
- **C1 transcript line only with a local exec env**: CC's own rule when diskless.
- **A1 / S1** no `claude`, `claude-code-guide`, `statusline-setup` agents, no bundled skills: CC
  product features (agent view, its docs, its status line, its settings files). Owner question 2.
- **S6 `run_skill_script`**: opt-in, off in every shipped host. Owner question 3.
- **T4 notes / response / follow-up / timeout** in `AskUserQuestion`: front-end dialog features
  noeta's front ends lack. Owner question 4.
- **N7 prompt images** named, not attached. Owner question 5.

## Open questions (owner)

1. E5: keep no rc snapshot (recommended), add an opt-in command prefix like pi's, or snapshot?
2. Ship noeta's own versions of CC's generic bundled skills (recommended: only
   `workflow-authoring`, replacing the reference inlined in `Workflow`'s description)?
3. `run_skill_script`: delete, or keep opt-in?
4. `AskUserQuestion` notes from front ends, and a typed way to skip one question in the TUI/ACP?
5. MCP prompt images through every front end's command path, or keep the text line?
6. CC lets `WebFetch` of its preapproved documentation hosts through without asking; noeta still
   asks per domain. Follow CC?
7. ACP sessions: set `interactive: false` with a prompt tool as CC's SDK-based adapter is?
8. A workflow `agent({schema})` with a custom agent whose `tools:` allowlist drops
   `StructuredOutput` fails; exempt the tool in the kernel's agent filter?
9. Plan naming: CC names only when the session has no name yet; follow (first approved plan only)?
10. A missing `--permission-prompt-tool` MCP tool: exit at start as CC does?
11. A shell read of a file a `Read` deny rule covers still brings its nested instruction files (CC
    skips it); the instructions plugin would have to read the permissions plugin's rules. Match?

## Plan

- [x] Environment and git: E1-E5
- [x] Reminders: R1-R4
- [x] Skills and agents: S1-S7, A1, M1
- [x] Tools: T1-T9
- [x] Compaction: C1-C2
- [x] Rule catalogue: K1; pi/dsh notes: K2
- [x] Found while closing: N1-N8, P1-P4, Q1-Q3
- [ ] Gates: `make check`, `make parity`, `make cluster`, `make e2e`; eval

## Acceptance criteria

1. Every item above is matched (with a test) or has a Decisions entry and a
   `parity/deviations.md` row; the "Not yet matched" section is gone.
2. The rule catalogue's rows named in K1 are correct; reminders, compaction and sub-agent rows
   carry noeta's status.
3. `make check` green, `make parity` 0 mismatches, `make cluster` and `make e2e` green; the
   eval shows no regression outside the known-noisy scenarios.

## Risks

- `StructuredOutput` replaces a provider feature with a tool: a model that never calls it
  ends with no structured value (CC nudges, then fails the run after its retries).
- Watching up to 5,000 read files costs one `stat` each per model call; on a remote exec env
  that must be one batched command, not 5,000.

## Progress log
- 2026-11-02 Created from the phase-1 audit; `make check` green on 95dce942.
- 2026-11-02 Work split over branches `ctxc-env`, `ctxc-mcpweb`, `ctxc-headless`, `ctxc-askhooks`,
  `ctxc-skills`, `ctxc-fsmisc` (each `make check` green, `make parity` 0), merged with `--no-ff`.
- 2026-11-02 The rule catalogue's new `noeta today` column on reminders / compaction / sub-agent /
  side-request rows found 13 more contradicts/missing rows (N1-N8, P1-P4); branches
  `ctxc-round2`, `ctxc-compact` matched them; `ctxc-shellreads` matched Q1 found by P3.
- 2026-11-02 Friction: merges conflicted on the request golden (re-recorded, hashes only),
  `stream_json` / `_stream_json_control` (both sides kept), `title.py`, CONTEXT Layers (budget).

