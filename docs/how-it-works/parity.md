# Matching Claude Code

What the model sees in noeta follows Claude Code: the tool names and schemas, the order and
roles of the prompt's parts, the reminders, compaction, the shape of the request a provider
receives. The machinery underneath (the event log, recovery, many machines) is noeta's own. This
page explains why the line is drawn there, what exactly is matched, how `make parity` checks it
against Claude Code's recorded requests, how `make eval` checks that models still behave well,
and where noeta differs on purpose.

## Why the model-visible part is a contract

Models are trained and tuned against the tools and prompts of the most-used coding agent. Their
strongest priors are on its tool names (`Read`, `Edit`, `Bash`, `Agent`, `ToolSearch` …), its
schemas, its message shapes and its conventions (`<system-reminder>`, task notifications, the
compaction summary). A harness that invents its own pays for it in model quality, and its users
must learn a new set of files and settings. So ([ADR-003](https://github.com/initxy/noeta/blob/main/docs/adr/003-claude-code-is-the-model-visible-contract.md)):

- What the model sees follows the latest Claude Code default configuration. A difference is a bug
  unless it is listed, with its reason, in `parity/deviations.md`.
- The implementation underneath is free to differ where noeta's is better.
- Claude Code's wording is allowed where it serves that parity
  ([ADR-020](https://github.com/initxy/noeta/blob/main/docs/adr/020-claude-code-wording-is-allowed-where-it-serves-parity.md)):
  models are tuned on the texts as Claude Code words them, so rewording a text only to make it
  different risks behaviour for no gain. Prompt texts are judged by the behaviour they produce
  (`make eval`, `make parity`), not by their wording's origin.
- noeta reads only its own places (`~/.noeta`, `.noeta/`, `NOETA.md`, `AGENTS.md`), never
  `~/.claude`, `.claude/` or `CLAUDE.md`, but the formats inside are Claude Code's: instruction
  layering, settings, skills, agent files, hooks, `mcp.json`, plugin directories.

Rejected: an independent prompt and tool design (measurably worse with current models, and a new
surface for users), and verbatim copies of everything with no record of where noeta must differ.

## What is matched

| Area | What follows Claude Code |
|---|---|
| Tools | Names, input schemas, descriptions, which tools are deferred, the order of the tool list, `DeferredToolPlaceholder` |
| System prompt | An identity block and the main prompt as two cached blocks; the main prompt's sections and rules; per-model paragraphs |
| Conversation layout | Instruction files and the memory index as host text in the first user turn; the environment, deferred-tool list, date and budget as mid-conversation system turns; a message sent during a turn framed inside the tool results' turn |
| Reminders | Their texts, when each is sent, how they are tagged (`<system-reminder>`) and joined |
| Request envelope | `max_tokens` from the model catalog, thinking and effort parameters, the beta header set, `context_management`, `metadata.user_id`, the cache-mark positions |
| Long outputs | The 50,000-character per-result ceiling, the 200,000 per-reply budget, the `<persisted-output>` preview |
| Compaction | The threshold numbers, the summarize instruction and its fallback call, the kept tail, files read again, the summary's framing, the rapid-refill and failure breakers, the summary written ahead, clearing old results, idle compaction |
| Side requests | The title request, the `WebFetch` page digest, the auto-mode permission classifier |
| Sub-agents | The `Agent` tool, completion notices, the hand-back |
| Files and settings | Skills, agent files, commands, hooks and their events, settings keys, MCP config, Claude Code plugin directories |

The rules behind each prompt text are restated in noeta's words, with hashes of the source texts,
under `parity/rules/` (the main prompt, compaction, reminders, side requests, sub-agents, the
classifier, and one file per tool).

## How `make parity` works

`make parity` measures how what noeta sends differs from what Claude Code sent in the same
scenario. It runs offline: noeta talks to a local fake Messages endpoint that answers with the
model turns recorded from Claude Code (`scripts/parity/`).

<Diagram name="parity-replay" caption="Recorded once from Claude Code; replayed against noeta on every run" />

**Recording** (outside the repository). `drive.py` types each scenario's steps into Claude Code in
tmux and answers each prompt with the key the step names (`1` yes, `2` yes and don't
ask again, `Escape` no; the highlighted first choice by default); `record.py`, a proxy between Claude Code and an
upstream Messages API, keeps every request and reply. The baseline is an API-key user on default
settings. The recordings stay in `$NOETA_PARITY_RECORDINGS` (default `~/cc-recordings`), never in
the repository. The scenarios (`scripts/parity/scenarios.json`):

| Session | What it covers |
|---|---|
| `m1` | Auto mode (Claude Code's default): the main tour, an interrupt, plan mode, `/compact` |
| `m2` | Default permission mode: approve, "don't ask again", deny, and a sub-agent's approval |
| `m3` | Automatic compaction on a small window (100K × 30%) |
| `m4` | `--continue`: `m1` resumed in a new process |
| `m5` | Auto mode with a project `PreToolUse` hook: a failing call, background `Bash` and `Monitor`, `TaskStop`, an irreversible action for the classifier |

**Distilling.** `distill.py` turns the recordings into a contract under `parity/<version>/`
(today `parity/2.1.312/`): per request, the parameter keys and values, the beta header values and
the header names, the message layout, the cache-mark positions, and each text's kind with its
length and hash; `tools.json` with each tool's schema verbatim but every description in it (the tool's and its
fields') reduced to length and hash; `texts.json`; and `script/`, the model's side of each scenario. Claude Code is
closed source, so only interface facts are committed, not its prose.

**Replaying.** `replay.py` starts the fake endpoint and runs noeta as the `noeta` command would:
the default agents, its real Anthropic provider, a fresh store and `NOETA_HOME`, the scenario
project (a small git repository with a `NOETA.md`, a skill, an MCP server) in a scratch directory,
every tool run for real. Main-loop calls are answered by step and position, sub-agent calls by
their opening prompt, compaction calls with the recorded summary, the classifier the way the
recording's classifier answered. Approvals are answered with the step's keys as the recording's
driver answered Claude Code (`Escape` denies, `2` allows always, anything else allows), and
questions get their first option. Every request noeta makes is kept in `parity/out/`.

**Comparing.** `compare.py` checks each of noeta's requests against Claude Code's at the same step
and writes `parity/report.md`: one line per difference, ranked by category and severity, each with
a short id. A difference accepted in `parity/deviations.md` (by its id) is not counted. The run
exits 1 while any mismatch is left. Against Claude Code 2.1.312 the report reads 0 mismatches,
with 11 accepted deviations not counted.

`own_words.py` also runs: an advisory report of where noeta's texts repeat a long run of Claude
Code's recorded texts, for a licensing review. It never fails ([ADR-020](https://github.com/initxy/noeta/blob/main/docs/adr/020-claude-code-wording-is-allowed-where-it-serves-parity.md)).

`make parity` is not part of `make check` (it needs the scenario project and takes minutes), but it
runs on every change to model-facing text or provider wire behaviour. Inside `make check`,
`tests/agents/test_request_golden.py` locks the default request bytes. A new Claude Code release
can open gaps; they are closed against new recordings (`scripts/parity/README.md` has the steps).

## How `make eval` works

Matching Claude Code's bytes does not prove a model behaves well with them, especially a model
that is not Claude. `make eval` (`evals/`) runs prompt-regression scenarios against a real model:

- 37 scenarios (`evals/scenarios.py`), each a small workspace and a prompt, scored 0 to 1 from the
  trace: which tools were called, what the files look like afterwards, and where needed a model
  judge. Examples: `read_over_shell_cat`, `edit_not_write`, `parallel_independent_reads`,
  `injection_in_file`, `ask_before_ambiguous_delete`, `honest_test_failure`,
  `compaction_continuity`, `skill_activation`, `memory_recall`, `plan_mode_read_only`,
  `deny_rule_respected`, `hook_block_adapts`, `mcp_tool_use`.
- Each scenario runs several trials (3 by default; `make eval-baseline` runs 9 and rewrites
  `evals/baseline.json`). The run fails when a scenario falls below its baseline by more than its
  tolerance (0.34 per scenario, 0.4 for `hook_block_adapts`, 0.05 overall, recorded in the baseline
file).
- Credentials come from the repository's `.env` (`NOETA_LIVE_BASE_URL`, `NOETA_LIVE_API_KEY`,
  `NOETA_LIVE_MODEL`); `--provider` picks `anthropic`, `openai_compat` or `responses`.

Parity says noeta sends what Claude Code sends; eval says the model still does the right thing
with it. A prompt change needs both.

## Where noeta differs on purpose

`parity/deviations.md` lists every accepted model-visible difference, over a hundred rows, each
with its reason and what pi and DeepSeek Harness do for the same thing. The reasons fall into a
few groups:

- **The kernel forces it.** Tools run after the reply is logged, not while it streams (log-first
  crash safety). A tool started before a crash answers "outcome unknown" unless it can tell its
  own outcome. Waits, timers and `Workflow` runs survive restarts and move between machines.
  Results go back in call order.
- **Durable tasks have no "session".** Changed instruction files are shown again with any new
  message, not only on resume; the git snapshot is given again when the branch moved between
  turns; the date-change note never goes backwards; the USD budget counts the whole task tree from
  the logs.
- **Many machines and remote exec envs.** The environment block describes the exec env, not the
  worker; the compaction summary names the transcript file only on a local exec env; a hook's
  `transcript_path` is empty because the conversation is the log.
- **Servers run unattended.** A `Stop` hook that blocks eight times in a row is let through; a
  model the catalog does not know gets conservative limits; `Write` refuses content over 8 MB
  because every call's input is logged.
- **noeta's own places and product.** `NOETA.md` where Claude Code says `CLAUDE.md`, `.noeta/`
  where it says `.claude/`; no billing header block (so the system cache marks sit one block
  earlier); no `LSP` tool, bundled skills or output styles yet; one fixed identity line.

When a difference has no row, it is a bug.
