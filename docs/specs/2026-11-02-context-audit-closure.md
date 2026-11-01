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

### Rule catalogue
- **K1** Stale `tool.Skill.4/.5/.9`; wrong "covered" on `main.env.8`, `main.env.10`,
  `tool.mcp.6`; no noeta status on the reminders, compaction and sub-agent rule files.
- **K2** pi / dsh notes on `parity/deviations.md` rows that lack them.

## Decisions

(filled in as items settle)

## Plan

- [ ] Environment and git: E1-E5
- [ ] Reminders: R1-R4
- [ ] Skills and agents: S1-S6, A1, M1
- [ ] Tools: T1-T9
- [ ] Compaction: C1-C2
- [ ] Rule catalogue: K1; pi/dsh notes: K2
- [ ] Gates: `make check`, `make parity`, `make cluster`, `make e2e`; eval if prompt text changed

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
