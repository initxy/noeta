---
status: active
created: 2026-10-29
branch: best-of-three
---

# Best of three: Claude Code, pi and dsh

## Goal

Every part of what the model sees (the context) follows the best of Claude Code (CC),
pi (pi-mono) and DeepSeek Harness (dsh), with CC as the default; every place noeta differs
is written down here with how the others do it and why noeta's way is better, backed by an
eval or a measured token count. `make parity` shows no difference that is not marked as an
intentional deviation. Every extension point of pi and dsh is compared in
`docs/extending.md`, and writing a plugin takes no more steps than in pi. The eval covers
every feature area with DeepSeek V4.1, and a current public coding-agent benchmark is run
and reported in the README.

## Non-goals

- Features outside the three kinds noeta builds: what CC has, what running on several
  machines needs, and the extension mechanism itself. Anything else is left to plugins;
  unclear cases are asked, not guessed.
- Pushing, releasing, or touching another program's data (owner sign-off).
- Reading `~/.claude`, `.claude` or `CLAUDE.md`; uploading anything.

## Context

- CC baseline: 2.1.312, recorded in `parity/2.1.312/` (`scripts/parity/README.md`); accepted
  deviations in `parity/deviations.md` (ADR 003).
- pi: `badlogic/pi-mono` @ e7a0df2 (2026-10-29). dsh: `deepseek-ai/DeepSeek-Harness` @ f3803c96
  (0.2.2-alpha.2, 2026-10-28). Both read from shallow clones, never vendored.
- Eval model: DeepSeek V4.1, gateway id `opensource/deepseek_v4_1_flash` (the gateway's only
  V4.1 model; it answers as `deepseek-v4.1-flash`), Anthropic wire.
- Starting point: local main @37fd9997, `make check` 2424 passed.

## Decisions

- **Instruction re-read trigger (B8).** CC: re-reads instruction files on `--continue`/resume
  and puts the changed ones before the resumed prompt. pi: re-reads only on `/reload`. dsh:
  announces updated nested files ("Updated instructions from:"). noeta: re-reads the files shown
  so far with every new message and shows the changed ones after that message. Why: a durable
  task has no resume moment (it sleeps between messages and may wake on another worker), and a
  note for a message lands after it in the append-only log. Evidence: zero tokens when nothing
  changed (digest compare); `parity/deviations.md` row; test
  `test_a_changed_instruction_file_is_shown_again_with_the_next_message`.
- **List-change notes (T2, B7) are a subset of CC's.** CC's deferred-tools delta also covers
  restored/retracted tool definitions, a denied-tool lead and needs-auth servers, and compresses
  long name lists; noeta tells joined, returned and removed names, and MCP server instructions
  joined/changed/removed. Why: the rest belongs to CC features noeta does not have (definition
  retraction, the auth-stub listing is already a tool), per "no features beyond CC's". MCP
  server instructions are cut at CC's 4,096 characters plus "… [truncated]" (bundle: the
  instructions go through the MCP description cut, `u$()` =
  `CLAUDE_CODE_MAX_MCP_DESCRIPTION_LENGTH ?? 4096`; no cut for first-party HTTP servers, which
  noeta does not have).
### @-mention placement (D2)
- Others: CC attaches the Read call and result as meta messages hoisted ahead of the person's text; pi's CLI inlines `<file>` text into the prompt; dsh attaches nothing and tells the model to read the path.
- noeta: CC's two meta blocks, read in the task through `Read`, logged after the person's message and moved ahead of its text when the request is built (the fs plugin's `before_model`, by each attachment's content digest in its record), as CC sends them. (Review fix, 2026-10-29: first shipped after the text; the "can't move it" reason held only for the log, not the request.)
- Evidence: `tests/plugins/fs/test_mentions.py`; DeepSeek V4.1, edit_not_write with and without `@shop/pricing.py`, 9 trials each: Read calls on the file 0.89 -> 0, requests 4.22 -> 2.89, input tokens 44.8k -> 30.9k, score 0.92 -> 1.00.

### @-mentions read only what the model could read unasked (review-p1-safety P1)
- Others: CC expands mentions only in what the person types into their own terminal, and checks only deny rules (`LYn(path, ctx, "person")`); no PreToolUse hook runs for an attachment. pi inlines what its CLI user names; dsh attaches nothing.
- noeta: a file is attached only when the task's gates would let the model's `Read` of it through without asking; an Ask (outside the working directories, through a link out of them, a rule) names the file instead, so the model's own `Read` goes through approval; a deny leaves it out. A message carrying a channel's frame (`from_channel`, the `<channel source=` mark `framed` puts on what an outside sender wrote, anywhere in the text so a sender cannot close it early) is not expanded; there is no other origin mark (a channel's text is posted as `human`), so `deliver` frames every message not framed yet (`source` / `sender` arguments, default `channel`), as CC's channel messages always arrive framed (re-review P3: safer than a docstring rule a channel could miss). `~` is expanded only with `FileTools(home=)`, which the `noeta` command sets for front ends (`own_tasks_only`), never for `serve` / `worker`. The hooks plugin's gate is not run for the attachment's `Read` (CC's behaviour): it runs the person's commands, and with the allow-only rule and owner-only expansion it can no longer be the only thing between a third party and a file, so skipping it opens nothing a hook guarded against for anyone but the owner, who can read the file anyway. Other plugins' gates still run (a host's own gate keeps its say).
- Why: noeta takes messages from servers and channels; CC's deny-only check would let any chat member read the worker's files (`~/.ssh`, `~/.noeta`) with no one approving.
- Evidence: `tests/plugins/fs/test_mentions.py` (outside / `../` / link in default, plan, acceptEdits; channel frame incl. an early `</channel>`; `~` with and without `home`; no hook command; directory listed whole), `tests/cli/test_config.py::test_only_a_front_end_names_the_persons_home_for_mentions`; `parity/deviations.md` Attachments row.

### Changed-file snippet source (B6)
- Others: CC diffs against the content it keeps for every read file; pi and dsh send no such note.
- noeta: the changed lines are worked out against the model's last whole `Read` result still in the conversation; without one the note says they are not shown. The snippet itself is CC's (`oKs`): 8 context lines, blocks one after another, cut at 8,192 characters with `... [N lines truncated] ...`; a file last read with an offset or a limit gets no note, as in CC.
- Line numbers: those of the file now; CC numbers each block from its start in the old file, which is off after an earlier block that changed the line count. A number the model cannot find in the file is the one thing numbering must avoid.
- Why: the log is the only state a task carries between workers; a stored copy of every read file would double the log for reads.
- Evidence: `tests/plugins/fs/test_changed_reminder.py` (a change shown after the step that made it; `Edit` with no new `Read`; no note for a windowed read; 8 lines; the cut).

### Date without the user's zone (C4)
- Others: CC uses the client's zone, else a UTC-labelled date (`assumedUtc`); dsh stamps the requester's zone; pi gives no date.
- noeta: same as CC: the task config's `timezone` (front ends send it), else the UTC date marked as UTC's, in noeta's own words.
- Why: a server's own zone is a wrong fact about the user.
- Evidence: `tests/plugins/context/test_date_change.py`; the full DeepSeek V4.1 eval (which sends no zone, so every request carries the UTC form) scored 0.927 overall against 0.919 on main.

### Smaller recorded differences
- Visible thinking does not count as speaking for the quiet nudge; a non-Claude model without `coauthor_email` gets no attribution note. Each has its `parity/deviations.md` row and reason.

### Extension points (Phase 2)
- Scope: the item-by-item review (pi e7a0df2, dsh f3803c96) found nine points to add (CC has them, multi-machine needs them, or they are the mechanism); the owner answered the open five on 2026-10-29: userConfig/`settings.json`, Python plugins shipping `skills/` `commands/` `agents/`, `InstructionsLoaded`, open model families yes; `statusLine` no (left to plugins). Every other row is have or won't, with its reason, in `docs/extending.md`.
- `--plugin-dir`: the person's own flag, so it loads for this run regardless of project trust (the machine policy still applies); refused with `--server`, whose plugins are the server's. A plugin file that joins no agent gets a notice, not an implicit `add_to`: one rule for every plugin place.
- Side-call headers (ADD-2). Others: pi and dsh hand plugins a model registry; CC's WebFetch calls a small model. noeta: `ToolContext.complete` is `TaskView.complete`, so `WebFetch`'s digest no longer carries the headers a hook put on the main request, as title, recall and summary calls never did. Why: those headers exist only in the process that built the main request; a durable copy would put credentials in the log. A host that needs them on every call sets them on the provider or passes `digest=`. Verified before removing: WebFetch's cache fell back to the configured model with no headers on a worker that had not built the request (after an approval or a crash), and had no call at all without an earlier hook there; compaction's did not break across workers (overflow follows its request in the same step) but held the request as seen in its own `before_model`, before later extensions changed it (test `test_overflow_summarizes_the_request_as_it_was_sent`).
- Tool progress (ADD-3) fits the live path: three `Delta` fields (`call_id`, `tool`, `elapsed`), one `ToolContext` field and a closure in the runner; nothing recorded. Across Postgres a progress text crosses as its tail (it replaces, so cutting it into pieces would show only the last). stream-json sends CC's `tool_progress` (no output in it, as CC's SDK) always, not only with `--include-partial-messages`. First user: foreground `Bash` (after 2 s, then every second, its output's last 4 KB), CC's live Bash output.
- userConfig (5a). Others: CC asks when a plugin is enabled, keeps secrets in its credential store (`pluginSecrets`), fills non-sensitive values into skill text. noeta: asks on `noeta plugin add` / `noeta plugin configure` (or `--config KEY=VALUE`); values in `~/.noeta/settings.json` `pluginConfigs` (CC's key in noeta's copy of the file, ADR-004), a sensitive one in the OS keyring with only a `{keyring:}` reference stored; filled into commands, agent prompts, MCP servers and hook `args`, not skill bodies. Why: a reference keeps secrets out of the task's plugin record; skill bodies get neither this nor `${CLAUDE_PLUGIN_ROOT}` yet, so both wait for one change. A plugin's `settings.json` `agent` (and the person's own `agent`) picks the agent front ends start with; `statusLine` keys are skipped.
- `InstructionsLoaded` (5c). A file shown again because it changed fires `session_start` (CC's reason when it reloads on a resume; noeta re-reads with each message). `compact` fires from `Compacted` and the instructions plugin's recorded files, so it fires even with re-attaching turned off. The hooks plugin reads the instructions plugin's records (CONTEXT "Layers").
- Model families (5d): any lowercase identifier, not only registered provider names (those are wire names, `openai_compat`); a near-miss of a known family is still refused.
- Python plugin parts (5b): only `skills/`, `commands/` (`/<directory>:<command>`) and `agents/`; hooks and MCP servers stay Claude Code plugins' (asked for three).
- Maintenance: `Agent.inherit_model` / `permission_mode` / `isolation` move to `Agent.extra` in CC's spelling (`model: inherit`, `permissionMode`, `isolation`); only the subtasks plugin read them.
- Review fixes (2026-10-29, review-ext-points): a plugin's option references are checked per MCP server and per hook when the plugin is read, so a secret one machine cannot read (keyring lacking it or no backend, a variable unset) leaves out only what uses it, with a notice naming the plugin, logged on a worker; `PreToolUse` / `PermissionRequest` / `UserPromptSubmit` hooks refuse (exit 2, `onFailure: block`) instead of disappearing; `Stop`-like events are left out, not refused, because refusing a stop would loop the turn. Every hook of a plugin gets each option this machine can read as `CLAUDE_PLUGIN_OPTION_<KEY>`, as CC; one it cannot read is left unset with a notice, and only a hook that names it is left out or refuses. Plugin-authored hooks and MCP servers may name only `{keyring:noeta-plugin/<plugin>/…}`; `{env:}` stays allowed (CC expands environment variables in `.mcp.json`). `plugin add` removes what it copied when configuring fails. Readers skip unknown delta kinds; versions before this one cannot, so the CHANGELOG gives the upgrade order. The digest model stays the task's (`ctx.model`), not the main request's after `before_model`: that model is not in the fold, and adding it is a fold-version change for one edge case.
### Reply result budget order (S3)
- Others: CC persists the largest fresh results of one user message once they pass 200,000 characters together (`Udr`), freezing each choice by `tool_use_id`; pi and dsh have no per-message budget.
- noeta: the same budget and `<persisted-output>` form, decided per result as each call finishes (a running total shared by calls that run together), so the results that arrive past the budget are saved, not necessarily the largest; nothing is saved for the budget without a spill directory. The runner saves to its own file, `<call>.result.txt` (`result_spill_path`), and never saves again a result that already points at its saved whole (a tool's own `<persisted-output>`: Bash, MCP, WebFetch, WebSearch); before the review fix (review-p1-safety P1) it wrote the tool's preview over the tool's own `<call>.txt`, losing the full output.
- Why: each call's result is logged as it finishes (crash safety); a largest-first choice would have to hold every result of the reply unlogged, or rewrite logged ones.
- Evidence: `tests/core/test_result_limits.py` (six 45K results: four inline, two saved, total ≤ 200K; a self-saved result past the budget keeps its file whole; the runner's own file name); `parity/deviations.md` row.

### Fallback when a large result cannot be saved (S4)
- Others: CC keeps only the first part ("only the first X are shown"); pi keeps the tail of a command's output; dsh keeps head and tail.
- noeta: with no spill directory, keeps the head and the tail around an elision (`<truncated-output>`). When the runner's save fails (a spill directory that cannot be written) the result stays whole, as CC keeps it (`Er`: a failed persist returns the content unchanged), for the ceiling and the reply budget alike (review fix: it was cut to ~2K with no file to point to, against this spec's own "nothing is saved for the budget").
- Why: a build or test log's error is at its end; two of the three keep the end. Only a host without a spill directory reaches it (every built-in host sets one), so no eval was run for it.
- Evidence: `parity/deviations.md` row; `tests/plugins/fs/test_output_limit.py`; `tests/core/test_result_limits.py::test_a_result_whose_save_fails_stays_whole`.

### Sub-agent plan file (A6)
- Others: CC gives a sub-agent of a planning conversation the plan-mode text in its sub-agent form with the plan file `<session plan name>-agent-<agent id>.md` (`nT`); pi and dsh have no plan mode in sub-agents.
- noeta: as CC. The parent writes its plan name into the child's config with its mode (`plan_name`, `Permissions.inherit`; a grandchild keeps the root's name), and the child plans in `<plans dir>/<name>-agent-<task id>.md` with the sub-agent text; no plan tools. (The earlier "needs the parent's state" reason did not hold: the parent writes the child's config.)
- Evidence: `test_a_sub_agent_of_a_planning_task_plans_too`.

### Where a task's own files live across workers (review-p1-safety P2)
- Others: CC keeps tool results, plans and auto memory as files in its config directory (`~/.claude/projects/<slug>/…`, `plans/`), one machine; its diskless mode keeps none and refuses to save. pi and dsh run on one machine.
- noeta: they stay files, read and written through the task's exec env, at paths recorded in the task's log (the `<persisted-output>` path, the memory plugin's `dir`, the plan's `path`), so they follow the task: Read of the spill root and the recorded plan file asks for nothing (`TaskView.spill_dir`; CC's "Tool result files / Plan files … are allowed for reading"), the memory plugin lists and reads its directory through the env when the env is not this machine (`toolkit/context.read_task_text` / `task_files`). With a remote exec env nothing besides the env is shared; with the local env on a shared filesystem the four directories of noeta's home go on the mount (docs/server.md, which states it precisely).
- Not the store: the model reaches these files with Read, Edit and Bash (`grep` a saved log), which a store row cannot serve without a second file system in every tool; a copy of each saved output in the log would put megabytes into every fold and every `/watch` reader, the opposite of why it is saved.
- Cleanup: CC deletes after `cleanupPeriodDays` (30). noeta: a host sweeps its spill root at start and daily (`noeta/host/spill_sweep.py`): a directory whose task and tree ended and that was not written for 30 days goes; an open task keeps its files however long it sleeps, and a continuation (`history_from`) of an ended task still reads its recent ones, which removal at task end would break. A remote env's root is the host's to sweep.
- Evidence: `tests/host/test_spill.py` (read without asking; sweep), `tests/plugins/context/test_memory.py::test_with_a_remote_exec_env_memory_lives_in_it`, cluster `test_a_saved_output_is_read_back_on_another_worker` (every worker killed between the save and the Read; default mode, no approval).

### Image decode cap (review-p1-safety P2)
- Others: CC decodes with sharp (its own pixel limit); pi and dsh pass images through.
- noeta: an image whose header (or Pillow's opened size, for formats without a known header) names more than 50 million pixels is refused before decoding; Bash and MCP images are fitted off the event loop. Why: a 640 KB PNG of 13000x13000 px took 1.4 GB and 4.9 s, below Pillow's own 178M limit; 50M is ~7000 px a side, far over the 2000 px the model gets.
- Evidence: `tests/core/test_images.py`, `tests/plugins/shell/test_bash.py`, `tests/plugins/mcp/test_results.py` (decode thread).

### Memory carve-out and nested skills (review-p1-safety P2)
- Memory: CC's auto-memory carve-out is judged on the literal path, with a list of path segments it never allows below the memory directory. noeta judges where the write lands: the resolved path in the resolved memory directory, a memory directory inside the working directory reached through no link, nothing protected below it; a planted `.noeta/agent-memory/a -> ..` no longer writes `.noeta/agents/` unasked, and a link out of the memory directory no longer reads unasked. Plan mode still writes memory `.md` files, as the carve-out shipped (CC's check sits ahead of its mode checks; not re-verified per mode). Evidence: `tests/plugins/context/test_memory.py` (two link tests).
- Nested skills: CC skips a gitignored skills directory (`git check-ignore`, "Skipped gitignored skills dir") and lists dynamic skills after the others, keeping the first of a name. noeta does both (nested directories first in the merge, so any other skill of the name wins; only unshadowed names announced). Evidence: `tests/plugins/context/test_skills.py` (two tests).
### Pending A/B
- None left: S2, S4, M7, C7, D6 and B5 are decided ("A/B results" below).

### Skill usage ranking wiring (owner, 2026-10-29)
- Others: CC ranks the over-budget skill listing by decayed use across sessions, always on; pi and dsh do not rank.
- noeta: on by default in the `noeta` command (every front end and server): the skills plugin gets `usage_from`, the store's client, and ranks by `UsageRank`; an SDK host that builds agents itself opts in with `usage_from` or `rank`.
- Why: a plugin reads no other task by itself (`CONTEXT.md` Layers), and front ends import no plugin, so the client is passed as the plugin's argument where the store is opened.
- Evidence: `tests/cli/test_skill_usage_rank.py`.

### Parity replay usage
- The replay's fake model reported zero input tokens; with the threshold now anchored on reported usage (C2) it would never compact. It reports noeta's own estimate of each request's text, as a real model reports its count; `make parity` stays at 0 mismatches.
- The comparator names the opening of CC's too-large-file note (`names.json`), so it is split as its own kind instead of merging into the token-counter part, which is why the missing note (C9) went unseen; m3's contract re-distilled from the r2 recording changes only those layouts. A general "kind CC sends and noeta never does" check already existed (`diff_layout`); it could not fire on a part merged into another.

### Benchmark (Phase 4)
- Benchmarks: Terminal-Bench 4.0 (63 of 66 tasks; 3 need a GPU) and SWE-rebench 07-2026 (111).
  Why: TB4 is the current headline board with Claude Code as its reference agent and is not
  saturated; SWE-rebench is fresh (issues after the model's data) and cheap per task. TB 2.1 is
  the smoke loop only.
- Comparison: noeta against harbor's `claude-code` agent (CC 2.1.312) in one harbor job — same
  model (`opensource/deepseek_v4_1_flash`, gateway Anthropic endpoint), effort `high`, tasks,
  timeouts, interleaved trials, concurrency split evenly. Recorded requests agree on
  `max_tokens` 65536, `thinking` adaptive, `output_config.effort` high, no temperature; CC gets
  `CLAUDE_CODE_MAX_CONTEXT_TOKENS` = noeta's 1M window (2.1.312 honours it for non-Claude ids,
  else it assumes 200k). The one wrapper over CC: both agents' process trees are stopped at
  the agent timeout (harbor only cancels its `docker exec`; the agent kept editing during a
  same-container verify).
- Fairness of the environment: noeta installs into its own venv under `/opt/noeta-bench`
  from uploaded static uv, rg and a python-build-standalone CPython; the task's interpreter
  and packages are never touched.
- Nothing uploaded: harbor telemetry off; CC telemetry, error reporting, auto-updater,
  non-essential traffic and the WebFetch domain preflight off; local dataset copies (a
  registry run records a download at harbor's hub); LiteLLM's price-map download off; network
  logs of the smoke runs show only the gateway, the corp proxy (package and install
  downloads) and task-driven traffic. The gateway key is redacted from job dirs.
- Nothing else touched: each run builds every image through its own buildx builder, removed
  with all it pulled at the end; prebuilt task images come in via a run-local dataset copy
  whose Dockerfiles are `FROM <prebuilt>` (compose sidecars' `image:` too, every service kept),
  so they live in the trial's compose project;
  leftovers are removed only when compose labels or container mounts prove them the run's.
  Pulled images are never removed by name or digest: another program may hold the same one.
  Cost of that: disk peaks per task in flight (SWE-rebench median ~2 GB on docker's disk with
  the builder's state elsewhere), so concurrency is sized to free space.
- Measures: pass rate (setup failures counted, infrastructure errors shown apart), paired
  per-task difference with its standard error, tokens from each agent's own records
  (noeta: its store — sub-agents and compaction included; side calls such as titles are in
  neither agent's count), time, and cost at DeepSeek's own list price (read 2026-10-30).

- Runs (2026-10-30 to 11-01, one machine): SWE-rebench all 111 at the dataset's 50 min
  cap; TB4 54 of 66 at a 2 h cap (3 GPU tasks need CUDA >= 13.2; 8 compose tasks and
  `cad-model` fail their own reference solution); alwaysday1_max on 20 SWE-rebench tasks
  (seed 0, large images left out). k=1 per run, k=2 pooled where two runs exist.
- Headline is the pooled k=2 SWE-rebench difference, beside the same-time pair and the
  leak-excluded variant: one k=1 run moves by about 5 points (CC 47.7 % then 42.3 %).
- Leaked answers are judged by content, not by URL: a trial is leaked when a fetch, search,
  `curl`, `git clone` or `git log` on the task's upstream repo returned lines of the reference
  fix. Both agents do it (20 tasks over three runs); reported, not blocked.
- Project instruction files are not equalized: CC reads `CLAUDE.md`, noeta `NOETA.md` or
  `AGENTS.md` (noeta never reads `CLAUDE.md`). Reported with its effect both ways.
- Rate limits are counted from CC's `api_retry` events; the old regex over all stream lines
  counted "429" inside token counts and thinking (CC 284 where the real count was 20).
- The env-loss bug found by the first run (noeta's `Bash` passed only an allowlist of
  variables) was fixed on main (34f8acee); its noeta arm was rerun and CC's kept, plus one
  same-time run of both to separate the fix from gateway timing.

### A/B results (2026-10-29)
Owner rule: where it was unclear whether noeta's way or CC 2.1.312's is better, both ran on DeepSeek V4.1 (`opensource/deepseek_v4_1_flash`, Anthropic wire), 9 trials per scenario and arm; a tie (within noise) goes to CC unless noeta's saves a measured amount of tokens with no score loss. Driver (not committed; the two scenarios worth keeping are): `evals.engine.run_trial` with each arm's plugin settings or a patch for the run only; every model call counted, side calls included. Results: `evals/results/ab-*.json` (git-ignored). Input tokens are per trial, cached included.

| Item | Options | Score | Input tokens | Verdict | Change |
|---|---|---|---|---|---|
| S2 listing budget | noeta: 1 % of window, est. tokens, cap 4,096 / CC: window x chars-per-token x 1 % chars, no cap | 0.926 / **0.982** | 41.3k / 38.2k | CC | budget is CC's, in characters; `Skills(budget_chars=)` |
| S4 over-budget degrade | noeta: short form for all, then full by rank / CC: full or name only by usage | **0.907** / 0.676 | 41.7k / 41.1k | noeta | none (kept) |
| M7 recall default | off (CC default, noeta CLI) / on (selector each message) | 1.000 / 1.000 | 24.6k / 15.7k | tie: off (CC) | none |
| C7 git snapshot on a later turn | frozen / given again when branch or HEAD moved between turns | 0.889 / 1.000 (16/18 vs 18/18) | 91.8k / 87.4k | within noise; noeta's deviation, kept as the durable-task form of CC's resume trigger | `Environment(git_refresh=True)` default |
| D6 pasted-image path | none / saved file + CC's path note / shipped `[Image source: <path>]` | 0.439 / **1.000** / **1.000** | 179.6k / 39.3k / 49.6k | CC | fs plugin saves pasted images |
| C14 prune before summary | CC: summarize (C15's CC arm, one shared run) / dsh: prune results > 8,192 chars first | 0.847 / 0.961 | 749k / 1,582k | tie: CC | none |
| C15 trigger and kept tail | CC: 83.5 % of W, newest reply kept / dsh: 52 %, 16 % of W kept / pi: 92 %, 10 % kept | 0.847 / 0.472 / 0.902 | 749k / 623k / 878k | tie: CC | none |
| B5 plan-mode full text | noeta: own words, 2,933 chars, 665 tokens / CC: 2.1.312's text from the bundle, 4,886 chars, 1,016 tokens | 1.000 / 1.000 (27/27 each) | 103.1k / 113.6k (uncached 27.6k / 29.9k) | tie, noeta's saves tokens: noeta | none (kept) |

- **B5.** Three plan-mode scenarios, 9 trials each: `plan_mode_read_only`, `plan_mode_started`, and the new `plan_mode_multi_file` (retire `legacy_connect`: three callers found only by looking plus `shop/db.py`; scored on no workspace change, `ExitPlanMode`, a plan file, every file named in it, a verification step). The CC arm patched in 2.1.312's full text as its bundle renders it (Explore up to 3, Plan agents up to 1, its plan-file lines; "supercedes" kept). Both arms scored every trial; every multi-file plan named all four files and a verification step. Token count on the gateway (same request with and without the text): noeta's full text is 351 tokens shorter per copy; it is sent at plan entry and every fifth plan-mode text after, and stays in every later request of the task. Per trial, uncached input was 2.3k lower with noeta's text (t about -2.9; -2.7k and -3.2k on the two single-file scenarios), total input 10.5k lower (within noise, t about -0.75). Behaviour differed in one way: CC's text started an Explore agent in 26 of 27 trials, noeta's in 17 (8 of 9 on the multi-file task, fewer on the one-file tasks, where both texts say one agent or none is enough); no score moved. By the owner rule (a tie with a measured saving and no score loss), noeta's text stays. No `parity/deviations.md` row: the replay compares which reminders are sent and where, not their wording (own words allowed, ADR 020). Eval scenario `plan_mode_multi_file` (baseline row from the noeta arm); `plan_mode_started` re-recorded at 1.000 and its known-gap entry dropped (the fix it waited on is merged).
- **S2.** 82 skills in the style of a real install (24.4K characters of listing, several opaque names such as `kiln`, `sansi`, `vdd`), 12 prompts that each need one skill without naming it, score 1 when the first `Skill` call is the right one. Budgets worked out for a 1M window (noeta 4,096 est. tokens: 50 skills full, 32 cut to their first sentence; CC 30,000 characters: all 82 full). The capped arm lost on skills it shortened (`vdd` 0.56 vs 1.00) and ran longer when it missed; the cap's listing saving (about 2,000 tokens, cached prefix) did not show as fewer input tokens. Change: `listing_budget_chars` (`noeta/toolkit/tokens.py`), entries measured in characters as CC measures them. Only a 1M window was measured; on a 200K model counted at 3 characters a token the budget is now 6,000 characters (was about 8,000), so the listing degrades a little earlier there, and a CJK description now costs a character per character against the budget (about a token each), as in CC. The 0.926 vs 0.982 gap (8 vs 2 misses of 108) is itself not significant (p about 0.1); a tie goes to CC as well.
- **S4.** Same fixture, budget forced to half the roster (12.0K characters for both arms), a usage ranking on (the targets spread over it). noeta's arm gave every skill its first sentence and 34 their full line; CC's gave 38 full lines and 44 bare names. CC's lost every trial on targets it listed by name only when the name says little (`cuihuo`, `quill`, `vdd`: 0/27 vs 21/27). noeta's way stays: a deviation from CC with this evidence (row in `parity/deviations.md`; the replay never goes over budget).
- **M7.** CC 2.1.312 first: run against the gateway (throwaway `CLAUDE_CONFIG_DIR`, telemetry and non-essential traffic off) through `scripts/parity/record.py`, two memory files present: no selector request was sent, the `MEMORY.md` index went into the first user turn and the model `Read` the file. In the bundle recall needs flag `tengu_moth_copse` (default false) or `CLAUDE_MEMORY_STORES` (`aie()`; `rGs`). A/B: 8 memory files, 5 questions a memory answers and 1 it does not (a mix that favours recall). Equal scores. The tie was judged on uncached cost and on the question no memory answers, not on total input: recall saved one request on memory questions (total input 24.6k -> 15.7k, but uncached about equal, 10.2k vs 10.9k with the selector call), and cost a selector call on every message, more where no memory applies (31.7k -> 35.2k). By the letter of the owner rule (noeta's way saves tokens with no score loss) total input points to on; the uncached and per-message cost do not, so off stays the default, as in CC. Found on the way, and fixed: on this gateway the selector failed silently every message (`thinking: disabled` refused with HTTP 400; with that dropped, `output_schema` ignored and bare file names returned). The A/B harness worked around both; noeta now retries once without the field (remembered per model), reads listed names from a reply that is not the schema's JSON, and logs one WARNING per failure reason.
- **C7.** A deviation of noeta's, not CC's behaviour: CC re-announces its session context (git status among it) only when it changed at one of a few moments, a resume in a new process among them; inside one process its git status stays frozen. A durable task has no resume moment (it sleeps between messages and may wake on another worker), so noeta's stand-in: when a turn ends the branch and HEAD are noted, and at the start of the next turn, when someone moved them in between (another branch, new commits), the snapshot is taken again and given in that turn (role user, "Taken again when the latest message arrived; it replaces the earlier snapshot"). The model's own edits and commits inside a turn do not count. Cost: one `git rev-parse` at each turn's end and start (milliseconds locally, a `docker exec` each in a container). Evidence: the person switches branch and commits between two turns, then asks for a PR description or the current branch: 16/18 frozen (2 PR descriptions named the old branch without running git) vs 18/18; within noise (Fisher p about 0.5), so this is kept as the durable-task form of CC's resume trigger, not as a measured win. A first version compared the whole snapshot and fired on nearly every turn (the model's edits change the status; `make parity` 5 layout mismatches); a second compared the branch only (`rev-parse --abbrev-ref` applies to both arguments), which missed a commit on the same branch; both fixed. Row in `parity/deviations.md`; eval scenario `git_snapshot_after_switch`.
- **D6.** A logo pasted with "put this into the repo as assets/logo.png" (and a variant that also edits the README). Without a path the model searched the disk; 7 of 18 trials recovered the exact bytes (by digging through the task store), 9 of 18 hit the 300 s cap. Shipped: the file tools plugin saves each image of a new person message from inside the exec env (one script: directories 0700 and owned by the user, never a symlink; files 0600, exclusive create) under `<noeta home>/image-cache/<task>/` locally (readable in the sandbox, `HOME_ALLOW_READ`) or `<temp>/noeta-images-<uid>/<task>/` in any other env, and names each in CC's one line, `[Image source: <path>]`. At a later turn's start a file that is gone (another worker, a cleaned temp dir) is written again from the image in the conversation; the model is told if it lands elsewhere or cannot come back. A root task's directory goes when it ends, and task directories untouched for a week when a worker first saves an image (CC clears old image caches at launch). Rerun on the shipped one-line note: 18/18 (both scenarios). Eval scenario `pasted_image_into_repo`.
- **C15.** The C14 tasks and window (the CC arm is one run, shared with C14); each policy's trigger and kept tail as its fraction of a 200K window (CC fires at 167K and keeps the newest reply; dsh at 104K keeping 16 %; pi at 184K keeping 20K), on noeta's compaction otherwise (summary, restored files, the rapid-refill stop). dsh's arm failed every logs trial: at this scale its kept tail plus the fixed prefix refilled to the early trigger within three calls, three times in a row, so the rapid-refill stop ended the turn. pi's late trigger scored 8/9 on the logs task against CC's 7/9 and used more input (878k vs 749k per trial); within noise, so CC's stays. The scaled window inflates the fixed prefix (about 11K of 64K), which is what sinks dsh here; at 200K it would not thrash, but nothing measured shows it beating CC either.
- **C14.** Two long tasks on a 64K virtual window (trigger at CC's 83.5 %): 18 logs of about 6.5K tokens each, one code per log in the middle; 20 files to edit plus a list of their owners. dsh's pruner (results over 8,192 characters cut to the first 4,096 and last 1,024, summarizing only if still over) cut summaries on the logs task from 2.56 to 1.0 per trial, but the model then read the pruned files again (40 reads for 18 files) and input tokens rose from 0.69M to 2.05M per completed trial. CC's two misses were turns that ended right after a compaction; with 18 trials per arm, 1 vs 3 failures is within noise. CC's way stays. The edits task rarely compacted (the model read near the line it changed), so it says little here.

## Plan

### Phase 0: baseline
- [x] DeepSeek V4.1 eval of the existing 13 scenarios on main, 9 trials, archived.

### Phase 1: context audit (branch `ctx-*`)
- [x] Per-area comparison CC / pi / dsh / noeta: system prompt, reminders, environment and
      git snapshot, attachments and images, skills, memory, project files, tool definitions
      and deferred tools, compaction, sub-agents, plan mode, todos.
- [ ] Revert every unexplained difference to CC; adopt pi/dsh where better, each with numbers.
- [ ] Every remaining difference recorded below (others / noeta / why better / evidence) and,
      when the replay can see it, marked in `parity/deviations.md`.

### Phase 2: extension points (branch `ext-*`)
- [x] Item-by-item table of pi and dsh extension points in `docs/extending.md`, each with a
      verdict and reason.
- [x] Plugin authoring steps no more than pi's.

### Phase 3: eval coverage (branch `eval-coverage`)
- [x] A scenario for each area: skills, reminders, memory, compaction continuation,
      sub-agents and parallel, plan mode, todos, permissions and approval, hooks, MCP,
      prompt injection, tool selection, ask vs act.
- [x] 9 trials each on DeepSeek V4.1; results and baseline archived.

### Phase 4: benchmark
- [x] Pick one or two current benchmarks, with reasons.
- [x] Run noeta and, where possible, CC on DeepSeek V4.1; version, samples, runs and cost in
      the README (`bench/README.md` Results, summary in `README.md`).
- [ ] alwaysday1_max rerun of noeta 34f8acee on the same 20 tasks (in progress), then
      README update.

### Phase 5: wrap-up
- [ ] `make check`, `make parity`, `make cluster`, `make e2e` green on main.

## Acceptance criteria

1. Each context area follows the best of the three, or this spec argues with evidence why
   noeta's way is better; `make parity` shows zero differences besides marked deviations.
2. The extension-point table is complete, each row with a verdict and reason; a plugin takes
   no more steps than in pi.
3. Every feature area has an eval scenario; 9 trials each on DeepSeek V4.1, results and
   baseline archived.
4. The benchmark result with version, samples and cost is in the README.
5. `make check`, `make parity`, `make cluster` and `make e2e` are green on main.

## Risks

- The gateway rate-limits (HTTP 429) under parallel eval load; a 0 from a 429 is a harness
  failure, not a regression.
- DeepSeek V4.1 is weaker at tool use than the model the baseline was recorded on; a new
  baseline on DeepSeek is recorded before any comparison.

## Progress log
- 2026-10-29 Started from local main @37fd9997; `make check` 2424 passed.
- 2026-10-29 Baseline on main: `make parity` 0 mismatches (11 accepted deviations); `make cluster` 13/13; DeepSeek V4.1 eval 9 trials overall 0.919 (`evals/results/ds41-main-37fd9997.json`): every scenario 1.00 except delegate_parallel_review 0.11 (5 of 9 hit the 240 s trial timeout), honest_test_failure 0.89, concise_direct_answer 0.94. 522 requests, 6.7M input tokens (5.3M cached), 125k output.
- 2026-10-29 T3: ToolSearch `+word` also matches a whole word of the description, as Claude Code filters (`noeta/plugins/tool_search/search.py`).
- 2026-10-29 M5: the recall selector picks by subject and is sparing with [user]/[project] memories (`memory/recall`); `side.recall` rules added.
- 2026-10-29 M3: a recalled memory shows its first 200 lines / 4,096 bytes with a note naming the file to Read (`noeta/plugins/memory/extension.py`).
- 2026-10-29 M4: a recalled memory older than a day carries an age-and-verify note ahead of its path.
- 2026-10-29 M6: recall skips sub-agents and one-word (non-CJK) messages, and never offers a memory file the task already Read.
- 2026-10-29 S3: a skill listing line is capped at 1,536 characters + `…` (was 384 estimated tokens, which cut CJK descriptions at 384 characters).
- 2026-10-29 S12: the noeta-only `priority:` skill key is gone; over-budget keep order is rank, tier, name.
- 2026-10-29 S7/S9: a loaded skill is the base-directory line and its body (no "Activated skill" header or description, one blank line); the Skill result says the skill starts now and its text follows; the post-compaction block follows.
- 2026-10-29 S6: a skill whose listed text changed is listed again (digest kept in the listing record), under the same header as the first listing.
- 2026-10-29 S10: a skill named mid-message (`/<name>` not as the leading command) gets a skill-mention note (`reminder/skill_mention`).
- 2026-10-29 S11: a `.noeta/skills` / `.agents/skills` below the working directory joins the task when a file under it is touched, announced by name (directories recorded in the log).
- 2026-10-29 I3: an `@import` whose extension is not on Claude Code's text list is skipped (`TEXT_EXTENSIONS`, `noeta/plugins/instructions/files.py`).
- 2026-10-29 I2: a nested instruction file over max(40,000, 5 % of the window in characters) is cut at a line end with a Read-the-rest note (`noeta/plugins/instructions/extension.py`, `chars_per_token`).
- 2026-10-29 B8: instruction files shown so far are read again with each new message; changed ones are shown again under a re-read note (digests in the log; deviation row for the trigger and placement).
- 2026-10-29 T2: deferred tools that join, return or leave mid-task are told in a system note (`deferred_delta`, `noeta/plugins/environment.py`; shared `noeta/toolkit/deltas.py`); the last list named is in the log.
- 2026-10-29 B7 (agents): agent types that join or leave mid-task are told in a system note (`subtasks` plugin, types in the log).
- 2026-10-29 B7 (MCP): servers' `instructions` from `initialize` reach the model under `# MCP Server Instructions` and are withdrawn on disconnect (`noeta/plugins/mcp/instructions.py`; recorded in the server listing, told set in the log).
- 2026-10-29 ctx-skills-memory gates: `make check` 2446 passed, coverage 88.46 %; `make parity` 0 mismatches (11 accepted deviations). DeepSeek V4.1, 9 trials, 5 scenarios vs `ds41-main-37fd9997`: no_delegate_trivial_lookup, read_over_shell_cat, compaction_continuity 1.00 (=); edit_not_write 0.92 (-0.08: 3 trials renamed with `sed` through Bash, which the auto-mode guidance allows; correct result, no Write); delegate_parallel_review 0.00 (-0.11, 5 of 9 hit the 240 s trial timeout, as on main). No list-change or re-read note fired in any trial; the harness reports no regression.
- 2026-10-29 D1 (ctx-attach-env): every image a person sends is fitted once in `Client.create`/`send` (`noeta/core/images.py`, moved from toolkit so the kernel can use it), covering the TUI, ACP, stream-json and HTTP; an unfittable one becomes a one-line note. Tested over HTTP and stream-json.
- 2026-10-29 D2-D5 (ctx-attach-env): @-mentions moved from the TUI into the file tools plugin (`noeta/plugins/fs/mentions.py`): read in the task's exec env through `Read` (counts as read for `Edit`; image, notebook, PDF via Read; first 2,000 lines or a too-large reference past the cap; long PDFs named with a by-pages instruction), CC's Called/Result blocks as system-origin content, deny rules honoured, and a mention sent from a TUI attached to a server is attached on the server. Rules `rem.at-mention.1-6`; two deviations (placement after the text, directory cap).
- 2026-10-29 C3 (ctx-attach-env): `Platform:` comes from the exec env (`uname -s` for a non-local env; left out when it cannot be told), no longer the worker's `sys.platform`; the shell/OS omission is now a deviations row.
- 2026-10-29 C4 (ctx-attach-env): dates follow the task config's `timezone` (the person's IANA zone, sent by the TUI, ACP and `-p`; inherited by sub-agents; accepted over HTTP); without one the UTC date, marked as UTC's (CC's `assumedUtc` form, own wording), so a server never gives its own zone's date. Rule `main.env.13` updated.
- 2026-10-29 C5 (ctx-attach-env): the snapshot leaves out `Git user:` when unset and gives an empty commit list instead of `(none)`, as CC; noeta's line-boundary status truncation kept.
- 2026-10-29 C8 (ctx-attach-env): the co-author address comes from the catalog (`ModelSpec.coauthor_email`; Claude models default to `noreply@anthropic.com`); a non-Claude model with no address gets no attribution note instead of an `@anthropic.com` trailer. Deviation row updated.
- 2026-10-29 B3 (ctx-attach-env): the quiet nudge fires every 5 tool-only steps since the stretch began or its last nudge, at most 3 per stretch (CC 2.1.312 `silent_turn_reminder`, `jwn=3`/`$wn=5`); rule `rem.quiet-nudge.2-3` updated.
- 2026-10-29 B6 (ctx-attach-env): file-changed note checked before every model call (so right after a Bash/formatter step), as a system turn (order 70), CC's meaning (current state, don't revert, say so if wrong, otherwise no mention) in own words, with the changed lines numbered (diff against the model's last whole Read still in the conversation; then the file counts as read) or a not-shown pointer to Read. Removed the 'Bash does not note a changed file' row from Not yet matched; rule `rem.file-changed.2` extended.
- 2026-10-29 B9 (ctx-attach-env): hook text for the model is led by CC's labels: `<hook name> hook additional context: ` (UserPromptSubmit context had no label at all; SessionStart no longer names its source) and `<hook name> hook blocking error` for a tool hook's block; tool events name `<event>:<tool>`. Rule `rem.hook-context.1`.
- 2026-10-29 ctx-attach-env gates: `make check` green (2443 passed, coverage 88.40%), `make parity` 0 mismatches (11 accepted deviations). DeepSeek V4.1, 13 scenarios x 9 (`--out` in the session scratchpad, ds41-ctx-attach-env.json): overall 0.927 vs 0.919 on main (ds41-main-37fd9997); every scenario within one trial of main except delegate_parallel_review 0.22 vs 0.11 (8 of 9 hit the 240 s trial timeout, as on main); 494 requests, 6.2M input tokens. Against the committed baseline (another model) `make eval` reports delegate_parallel_review as regressed, as main does.
- 2026-10-29 Phase 2 started on `ext-points` from `best-of-three` @b9c7ca7a; `make check` 2465 passed.
- 2026-10-29 ADD-1: `--plugin-dir` (file, plugin directory, Claude Code plugin or a directory of them; TUI and `-p`) loads for this run; a plugin file that joins no agent is a notice; hello-plugin README fixed.
- 2026-10-29 5d: `register_models` accepts any lowercase family; near-misses of anthropic/openai refused.
- 2026-10-29 ADD-2: `ToolContext.complete`; `on_context_overflow(view, request)`; WebFetch's and compaction's per-process caches removed (what each broke: Decisions).
- 2026-10-29 5c: `InstructionsLoaded` hooks fire per instruction file shown (five load reasons, CC's fields).
- 2026-10-29 ADD-4: `Allow`, `Deny`, `Ask`, `Rewrite`, `ToolChanges`, `ToolUse`, `Recorded`, `TaskState` exported from `noeta`; the examples import from it.
- 2026-10-29 ADD-3: `ToolContext.progress` on the live path; TUI, ACP `tool_call_update`, stream-json `tool_progress`; foreground Bash is the first user. A timing race in its test fixed in a follow-up commit.
- 2026-10-29 5a: CC plugin `userConfig` (asked on `noeta plugin add`, `configure`, `--config`) and `settings.json` `agent`.
- 2026-10-29 5b: a Python plugin directory ships `skills/`, `commands/`, `agents/`.
- 2026-10-29 Maintenance: sub-agent fields into `Agent.extra`, `ToolContext.extras` dropped, the permissions/checkpoints record contract written down (CONTEXT "Layers", extending.md).
- 2026-10-29 docs/extending.md: full item-by-item table (have / added / won't, each with a reason), new hook rows, step count next to pi's (4 and 4; 1 command for a file kept elsewhere).
- 2026-10-29 Friction: an outside writer duplicated a block of `tests/plugins/context/test_date_change.py` in this worktree mid-run (F811 in `make lint`); not ours, restored, its diff kept in the session scratchpad.
- 2026-10-29 ext-points gates: `make check` 2488 passed, coverage 88.53 %; `make parity` 0 mismatches (11 accepted deviations); `make cluster` 13/13. No model-facing prompt text changed, so no eval run.
- 2026-10-29 Review fixes on `ext-points` (review-ext-points, P1 + all P2 + cheap P3): secrets checked per plugin part with notices and fail-closed guard hooks; keyring errors are messages, not tracebacks; `plugin add` rolls back; tolerant delta readers and the rolling-upgrade note; ACP ignores progress after a result; API changes under "Changed"; plugin files name only their own keyring secrets; old one-argument `on_context_overflow` still runs; Bash progress errors never fail a call; `__init__.py` in a plugin place hides nothing; settings file written atomically; `<plugin>:<agent>` resolved exactly; settings-file hooks set no `env`; dsh `--patch` cell corrected. Probe test `review/probe` scenarios now build with notices.
- 2026-10-29 Re-review fix: hooks get every option this machine can read as `CLAUDE_PLUGIN_OPTION_<KEY>` again (CC; a script reading its secret from the environment works, tested), an unreadable one is unset with a notice; a failed `plugin add` keeps options kept before; agent-file hooks set no `env`; `parity/deviations.md` Hooks rows for the fail-closed guard hook and plugin-own keyring references.
- 2026-10-29 C9 (ctx-compact-plan): a restored file that `Read` refuses whole for its size gets the too-large note (CC `compact_file_reference`) instead of nothing; candidates follow CC's read-file state, which a compaction resets to the files it restored in full (`restored_paths`, `seen_through` in the compaction record), so a noted file is not noted again. Comparator: `names.json` names the note's opening; m3 re-distilled from r2. Rules `compact.reattach.12-13`.
- 2026-10-29 C3 (ctx-compact-plan): rapid-refill breaker (CC `J9t=3`): an auto-compaction needed again within 3 model calls of the last in the same turn is a refill; the third in a row ends the turn (`autocompact_thrashing`). Rule `compact.trigger.7`.
- 2026-10-29 C2 (ctx-compact-plan): the threshold check is the newest reported usage plus the estimate since; the full estimate only before any usage. The parity replay's fake model now reports noeta's estimate of each request as its input tokens (it reported 0).
- 2026-10-29 C11 (ctx-compact-plan): skills given again after a compaction: newest invoked first, each cut to 5,000 tokens with a note, 25,000 in all (CC `Far`, `O9e`). Rule `compact.reattach.14`.
- 2026-10-29 S2/S3 (ctx-compact-plan): the runner caps every tool's text result at min(its `max_result_chars`, 50,000) and a reply's results at 200,000 together, saving the rest with `<persisted-output>` before logging; `Read` is exempt; WebSearch 100K -> 50K. Spill helpers moved to `noeta/core/large_output.py` (toolkit re-exports). S4 head+tail fallback kept with a deviations row. Rules `compact.results.1-2`.
- 2026-10-29 Memory saving bug (ctx-compact-plan): permissions refused every write to the memory directory (outside the working dirs) and the file tools' fence would too; now CC's auto-memory carve-out: `.md` writes and any read under the memory plugin's `dir` are allowed in every mode, and the fs write roots include it. Rule `main.mem.20`.
- 2026-10-29 Plan mode (ctx-compact-plan): a task created in plan mode now enters it on start with a plan file (it never got plan state, so no text and the model edited); a mode switch arriving with the prompt is told at once (only a typed `/plan <request>` waits a call, as m1); P1 cadence (CC `hTn` 5/5: full, none for 4 prompts, sparse, full every 5th); P2 re-entry note when planning again with the old plan file; A6 children inherit plan mode with the sub-agent form of the text. Rules `rem.plan-active.10-12`, `rem.plan-reentry.1`.
- 2026-10-29 A5 (ctx-compact-plan): the 16-Agent-calls-per-reply cap is gone (no such cap in CC; no kernel reason: each call stages its child in its own append). Rule `tool.Agent.23`.
- 2026-10-29 UsageRank (ctx-compact-plan, owner decision): the `noeta` command passes its store's client to every skills plugin (`usage_from`), so the listing ranks by use by default. Rule `main.env.17`.
- 2026-10-29 ctx-compact-plan gates: `make check` green (2481 passed, coverage 88.51 %), `make parity` 0 mismatches (11 accepted deviations). DeepSeek V4.1, 9 trials (eval-coverage scenarios run from a local copy of that branch's `evals/`, not committed), this branch vs `best-of-three` @b9c7ca7a: memory_save 1.00 vs 0.07; plan_mode_started (local variant of plan_mode_read_only created with `permission_mode: plan`) 0.78 vs 0.00; plan_mode_read_only 1.00 vs 0.89; background_subagent_result, skill_activation 1.00 both; compaction_resume_edits 0.93 vs 1.00 (every trial ends on `autocompact_thrashing`: the scenario's 24K usable window refills within 3 calls of each summary, which is where CC's breaker stops too; at a 40K window the scenario never compacts, 0.00 by its rule, so the scenario needs a window between the two). Against `ds41-main-37fd9997`: compaction_continuity, parallel_independent_reads 1.00 (=); delegate_parallel_review 0.11 (= main, 600 s trial timeouts).
- 2026-10-29 Review fixes (review-p1-safety, branch `ctx-fix-safety`; gates: `make check` 2541 passed, coverage 88.69 %, `make parity` 0 mismatches, `make cluster` 14/14): @-mentions attach only what the model's own `Read` could read unasked, never from a channel's message, `~` only on the person's machine, no hook command for the attachment, ahead of the person's text, directory listed whole; the runner never saves over a tool's own saved output and keeps a result whole when its save fails; spill files, plans and memory follow a task to any worker (Read of the spill root and plan unasked, memory through the exec env, ended tasks' old outputs swept after 30 days); image decode cap and off-loop decoding; memory carve-out judged on the resolved path; nested skills skip gitignored directories and shadow nothing; `make cluster` runs `python -m pytest` and fails on skips. The review's probe tests are real tests now (mentions, spill, memory).
- 2026-10-29 Re-review fixes (`ctx-fix-safety`): a task's spill directory is named by its id when that is a safe file name, else the id made safe plus a 10-hex hash (`task_spill_dir`), so two ids never share one; every save marks the directory with the task id (`.task-id`, `save_spilled` / `mark_spill_dir`) and the sweep goes by the marker only (no marker, or a task the store does not know: kept), so an open `chat:room1` task keeps its files. `deliver` frames every unframed message (re-review P3). Probe `test_probe_fix2.py` is `tests/host/test_spill.py::test_an_ended_tasks_old_saved_outputs_are_swept` and `tests/plugins/fs/test_mentions.py` (outside path and `~` on a server).
- 2026-10-29 ctx-fix-fidelity (review-p1-fidelity fixes, from `best-of-three` @0d96a993): changed-file snippet with CC's 8 context lines and 8,192-character cut, no note after a windowed read; new, edited and nested-found skills listed with descriptions before the next model call (one checksum command over the SKILL.md files per call; nested ones no longer recorded as listed); sub-agent plan file `<parent plan name>-agent-<task id>.md`; hook blocking feedback names the command (deviation row removed); Bash 30,000, Monitor and ListAgents 10,000, ScheduleWakeup 1,000, ReportFindings 256 declared on the tools; plan-mode cadence checked per model call; MCP server instructions cut at 4,096 (read from the bundle); the memory index read again with each message; the plan-mode rules cut from the full text restored. Pending A/B section added to Decisions for S2, S4, B5, M7, C7, D6. The directory @-listing cap is left to ctx-fix-safety.
- 2026-10-29 ctx-fix-fidelity gates: `make check` green (2523 passed, coverage 88.67 %), `make parity` 0 mismatches (11 accepted deviations, the hook-feedback row removed). DeepSeek V4.1, 9 trials, 9 scenarios (eval-coverage `evals/` run from a local copy, not committed), this branch vs `best-of-three` @0d96a993 (session scratchpad `fid/ds41-fix-fidelity*.json`): overall 0.919 vs 0.900; skill_activation, skill_follow, memory_save, memory_recall, plan_mode_read_only, plan_mode_started, mcp_tool_use 1.00 both; changed_file_reminder 0.73 vs 0.78 (every trial got the note and answered with the new value; the score differs only in whether the model also re-read the file, 3 vs 4 of 9); hook_block_adapts 0.53 vs 0.32 (PreToolUse text unchanged; base had more 420 s timeouts). 458 vs 515 requests, 5.6M vs 6.3M input tokens.
- 2026-10-30 Phase 4 harness on branch `bench-2026` (`bench/compare.py`, `bench/harbor_adapter.py`): smoke 2+2 tasks both agents graded; gateway took 32 parallel calls with no 429; review fixes (own venv, CC context window, store usage, WebFetch preflight, run-owned builder and cleanup). Full run not started.
- 2026-10-30 Phases 1–3 integrated on best-of-three (ctx-skills-memory, ctx-attach-env, ctx-compact-plan, ctx-fix-safety, ctx-fix-fidelity, ext-points, bench-2026; eval-coverage via main); each reviewed independently until no P0/P1. Gates @a461f768: `make check` 2563 passed; `make parity` 0 mismatches (11 accepted deviations); `make cluster` 14/14; `make e2e` 7 passed; DeepSeek V4.1 eval 29 scenarios x 9 trials overall 0.944 vs baseline 0.868 (`evals/results/ds41-best-of-three-19751ff9.json`, local), the known gaps memory_save and plan_mode_started 0→1.00; changed_file_reminder 1.00→0.64 traced to its scorer requiring a re-read the note now makes unnecessary (the note carries the changed lines, as CC's), rescored: 9/9 1.00.
- 2026-10-30 Eval results stay local (`evals/results/`, git-ignored: they hold machine paths); the baseline (`evals/baseline.json`) and the per-scenario numbers in this log are the archive.
- 2026-10-29 A/B of the undecided context choices on `ab-context` (S2, S4, M7, C7, D6, C14, C15; Decisions "A/B results"): listing budget is CC's (characters, no cap); short-form degrade kept; recall stays off; git snapshot given again when branch or commit moved; pasted images saved with their path; compaction stays CC's. New eval scenarios `git_snapshot_after_switch`, `pasted_image_into_repo` (baseline rows from 9 trials each). Tests get their own `NOETA_HOME` (one wrote to the user's). `make check` green, `make parity` 0 mismatches.
- 2026-10-30 Review fixes on `ab-context` (review-ab): pasted images readable in the sandbox (`HOME_ALLOW_READ`), private and symlink-safe, written again when gone, removed with the root task or after a week unused, named in CC's one line (rerun 18/18); git refresh compares with HEAD at the previous turn's end and reads the commit, not only the branch; C7 and S4 rows in `parity/deviations.md`; recall selector reads bare names, retries without `thinking` on a 400, warns once. Both kept scenarios 9/9 on the final code. Merged `best-of-three`; `make check` 2578 passed (coverage 88.75 %), `make parity` 0 mismatches.
- 2026-10-30 B5 A/B on `ab-plan-text` (Decisions "A/B results"): noeta's own-words plan-mode full text kept (tie at 1.000, 351 tokens shorter per copy, uncached input 2.3k lower per trial); no context item is left pending. New eval scenario `plan_mode_multi_file`; `plan_mode_started` baseline re-recorded.
- 2026-10-30 Final gates on best-of-three @bc170424 (all branches merged, incl. ab-context and ab-plan-text): `make check` green; `make parity` 0 mismatches (11 accepted deviations); `make cluster` 14/14; `make e2e` 7 passed; DeepSeek V4.1 eval 32 scenarios x 9 trials overall 0.953 vs baseline 0.911, no regression (`evals/results/ds41-final-bc170424.json`, local). `evals/baseline.json` re-recorded from that run (known gaps cleared: memory_save and plan_mode_started 1.00). Run cost: 1,665 agent requests, 23.0M input tokens (18.5M cached), 0.32M output.
- 2026-10-31 Bench run 1 (noeta 54121930 vs CC 2.1.312): SWE-rebench 45.0 % vs 47.7 % (-2.7, SE 4.3); noeta's `Bash` dropped the image's environment (22 of 111 trials set `CARGO_HOME`/`JAVA_HOME` by hand, 7 hit toolchain-not-found; CC 0). Fixed on main 34f8acee.
- 2026-11-01 Bench reruns with 34f8acee: SWE-rebench fix run 51.4 %; same-time run noeta 49.5 % vs CC 42.3 % (+7.2, SE 3.8); pooled k=2 +5.4 (SE 3.6), +7.1 (SE 3.6) without the 20 leaked tasks. TB4 near zero for both (`bench/README.md`). alwaysday1_max pre-fix -20 (SE 11.7, n=20); its 34f8acee rerun in progress.
- 2026-11-01 `compare.py summary` counts CC `api_retry` events (429/529) instead of every stream line matching "429".
