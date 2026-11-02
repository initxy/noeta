# Auto-mode classifier

Claude Code version: **2.1.312**. Baseline: session `m1` (API-key user, `auto` permission mode),
cross-checked with `m3` (same setup, auto-compaction). For an API-key user the classifier is a
**client-side** model call that Claude Code makes before running a tool call; the OAuth variant
`s1` makes no such call (its main request carries the same `safeguards` block and the check
runs server-side). `m2` (default mode, approval prompts) makes no classifier call and sends no
`safeguards` block.

Rules restate structure and decision logic in our own words. Claude Code's rule names, example
commands and example lists are deliberately not reproduced; noeta picks its own rule names.

## Sources

| source | sha256 (normalized, see README) |
|---|---|
| `m1/0006 system[1]` (classifier prompt, main session, 140239 chars) | `fcf7f7dc45d6ffef846877b1ca16c3c9f82155d52fa9cc541396cf8367495183` |
| `m1/0032 system[1]` (same prompt as seen from a sub-agent, 140834 chars) | `a0a1f648a6d29a137c46df32962b09da4a950fd63a2884f2e71c576676c87c7b` |
| `m1/0006 system[2]` (session-context block, 250 chars) | `93d9ffbcefce312fa73c0ac1dfa8820637b4548a5a12770098a96f4a20fbe42e` |
| `m1/0006 messages[0].content[0]` (CLAUDE.md preamble + body) | `70edd201a6dfda86ceaf1ea5be7bff7951d57f2602eca12c1ba09e124f76c927` |
| `m1/0006 messages[1].content[4]` (stage-1 trailing instruction) | `209bf6487d13c356b42ba98dc41baa1728da067e6487164dd83a31e25d00ae71` |
| `file:dump/auto-classifier-system.txt` (all three system blocks joined by `=====` lines) | `1e262fd599fd19a841c710a737498bf94b4fefd569a18d7727ca34c51c19d55d` |

`dump/auto-classifier-system.txt` is not byte-equal to `system[1]`: it is billing header +
`system[1]` + `system[2]` joined by separator lines. The 140834-char variant (`m1/0032`, `m1/0035`,
both sub-agent classifications) differs from the main-session text in exactly one place: an
extra soft-block entry listing the effective permission **deny rules** (see `cls.input.14`).

## When a classification is requested

| id | rule | noeta today |
|---|---|---|
| cls.when.1 | The classifier runs only in `auto` permission mode and only for an API-key user; it is one extra model request issued after the main model returns a tool call and before that call runs. | covered — an ask in `auto` mode is handed to the classifier before the call runs (`decision.py` `in_mode`, `extension.py` `resolve_ask`); noeta has no server-side variant, so it always runs client-side. Plan mode too, as the bundle's `useAutoModeDuringPlan` (default on, not in the recordings) where auto mode is available (planning from auto mode, a host starting tasks in auto mode, or a task that has been in it; `parity/deviations.md`): its asks go to the classifier except a file tool's write and plan mode's refusal of a tool that is neither read-only nor a shell tool, which the person answers (`in_mode` `plan_classifier`, `state.plan_classifier_on`, `Permissions(auto_during_plan=)`); the classifier is not told the mode is plan, as in Claude Code |
| cls.when.2 | One classifier request per tool call that needs a decision. When a response holds several tool calls, only the ones that need a decision are classified; the others still appear in later transcripts as context. | covered — one verdict per call id, recorded (`auto.py` `answer`, `st.classified`); calls that need none stay in later transcripts (`transcript_entries`) |
| cls.when.3 | Shell commands that a client-side read-only check accepts skip the classifier (observed: `cat`/`ls`/`echo` chains inside the project, a backgrounded `sleep; echo`, `find … \| xargs wc`). | covered — read-only commands inside the working directories are allowed before any ask (`shell_check.py` `shell_decision`) |
| cls.when.4 | Shell commands that write, run an interpreter or test runner, install, or reach outside the working directory are classified (observed: heredoc file writes, `pytest`, `python -m pip`, `uv venv/pip install`, a listing of a system directory outside the project, a `find … \| sed` pipeline). | covered — any other command asks, and in auto mode the ask goes to the classifier (`shell_decision`, `in_mode`) |
| cls.when.5 | Not classified: `Read`, `Edit` and `Write` inside the working directory or the session's plan/config directory (including an `Edit` of the project `CLAUDE.md`), `ToolSearch`, `Skill`, `AskUserQuestion`, `ExitPlanMode`, MCP resource reads (`ReadMcpResourceTool`). | covered — in-project edits are allowed in auto mode (`rule_check.py` `_edit`), reads inside allowed (`_read`), the listed tools are in `policy.py` `AUTO_SAFE_TOOLS` |
| cls.when.6 | Classified: an MCP tool call (`mcp__<server>__<tool>`), `WebFetch`, `Agent` (sub-agent spawn), and a sub-agent's final hand-back (`SubagentHandback`) before it is delivered to the parent. | covered — MCP tools default to `high` risk and ask; `WebFetch` asks unless allowed; `Agent` and `SubagentHandback` are low risk but not auto-safe, so classified (`rule_check.py` `_needs`); a blocked hand-back is delivered under auto mode's warning, not refused, and counts no denial (the bundle's `onBlock: "flag"`; `auto.py` `_handback_gate`) |
| cls.when.7 | Tool calls made inside a sub-agent are classified the same way, with the sub-agent's own transcript (its task prompt as the first user entry) and the sub-agent's effective deny rules. | covered — the sub-agent's own log is the transcript; deny texts add its denied tools (`auto.py` `_deny_texts`) |
| cls.when.8 | Classification continues after compaction; the transcript then starts from the compaction summary (`cls.input.10`). | covered — the transcript starts at the compaction summary (`auto.py` `_transcript`, `summary_at`) |

## Request shape

| id | rule | noeta today |
|---|---|---|
| cls.req.1 | System prompt has three blocks: the billing header line, the classifier prompt (one cache breakpoint), and a short session-context block (no breakpoint). The main prompt and identity line are not sent. | covered by a row — the billing header line is left out of every noeta request (`parity/deviations.md` Request envelope, `41a7944c`), so the prompt (cached) and the session block sit at `system[0]`/`[1]` (`classifier_request`); no main prompt |
| cls.req.2 | No tools are sent. `stop_sequences` is `["</block>"]`. `metadata.user_id` is the same device/session JSON as the main loop. | covered — no tools, stage 1 stops at `</block>`, and the request names the task tree (`root_id`), so `metadata.user_id` is the main loop's (`classifier_request`) |
| cls.req.3 | Messages: exactly two user messages. The first holds the CLAUDE.md context block (cached); the second holds the transcript followed by the stage instruction. | covered — instructions message (cached, `cache_after=[0]`), then the transcript message (`classifier_request`); the first is left out when there are no instructions |
| cls.req.4 | The second message is a list of text blocks: an opening `<transcript>` tag block, then the transcript split into chunks that grow append-only across calls, a closing `</transcript>` block, then the stage instruction. The last two transcript chunks carry cache breakpoints, so successive classifications reuse the cached prefix. | covered — `<transcript>` block, append-only chunks (`classifier.py` `chunks`), closing block, stage text; marks on the last two chunks (`cache_blocks`) |
| cls.req.5 | First stage-1 attempt in a session goes to a smaller, faster model (`claude-sonnet-5`) with `max_tokens` 64 and thinking explicitly disabled. | covered — `FAST_MODEL` at 64 tokens with thinking off (`classifier_request`); a non-Claude main model is used directly |
| cls.req.6 | Observed fallback: when that first request failed (HTTP 400 from the gateway, `m1/0005`, `m3/0004`), the same request was resent at once to the main-loop model with `max_tokens` 2112 and no `thinking` field, and every later classification in that session used the main model at 2112. | covered — a failed fast-model call is resent to the main model and the switch is recorded for the session (`auto.py` `_verdict`, `demoted`) |
| cls.req.7 | `2112` = 64 + 2048: the larger budget leaves room for reasoning before the verdict; the 64-token budget allows the verdict tag only. | covered — `THINKING_HEADROOM` 2048 on top of the stage budget |

## Stages

| id | rule | noeta today |
|---|---|---|
| cls.stage.1 | Two stages. Stage 1 is a fast, block-leaning screen; stage 2 is the full judgment that applies user intent and allow exceptions. | covered — `auto.py` `_verdict`: stage 1, then stage 2 only after a non-`no` |
| cls.stage.2 | Stage 1 instruction (trailing block): lean towards blocking; judge the full effect, not surface form; block if any rule could apply; do not apply user intent or allow exceptions; the reply must start with the verdict tag and carry no preamble or analysis. | covered — `classifier.py` `STAGE1_INSTRUCTION`, our words |
| cls.stage.3 | Because the stop sequence is `</block>`, a stage-1 reply ends right after the verdict word: observed replies are exactly `<block>no`. | covered — `STOP` on stage 1 only; `parse_stage1` |
| cls.stage.4 | A stage-1 `no` allows the call; nothing further is sent. Every recorded classification ended here (all `no`), so the stage-2 request was never observed. | covered — a stage-1 `no` returns at once (`_verdict`) |
| cls.stage.5 | Inferred (not recorded): a stage-1 `yes` triggers stage 2, which re-asks with intent/exception handling enabled and reads category and reason (`cls.out.*`). Mark as unverified until a recording shows a block. | covered — stage 2 at 8192 tokens, reasoning in `<thinking>` first, its verdict stands (`STAGE2_INSTRUCTION`, `parse_stage2`); not checked against a recording |

## Inputs

| id | rule | noeta today |
|---|---|---|
| cls.input.1 | First user message: a fixed preamble saying the content is the user's CLAUDE.md, that it may count as intent only when it explicitly authorizes the same operation on the same target, and that general encouragement to be autonomous never lowers the bar; then the full CLAUDE.md context (as the main loop sees it, including its own header) inside a `<user_claude_md>` element, each line indented two spaces. | deviation — preamble and two-space indent as described, inside `<user_noeta_md>` (parity/deviations.md noeta's own files); an untrusted workspace gets an `<untrusted_instructions>` block that grants no intent (`classifier_request`) |
| cls.input.2 | Transcript format: JSON Lines, one entry per line. A user entry is `{"user": "<text>"}`; a tool call is `{"<ToolName>": "<compact rendering of its input>"}`. | covered — `classifier.py` `_entry` (entries over 20,000 characters cut) |
| cls.input.3 | Assistant prose is not included in the observed transcripts (neither plain replies nor text that precedes tool calls). The prompt still defines how an `assistant` entry would be read (`cls.intent.7`). | covered — `transcript_entries` renders only tool calls from assistant messages |
| cls.input.4 | Tool results are never included. The classifier sees commands, not their output. | covered — tool results are skipped, except an `AskUserQuestion` answer (`transcript_entries`) |
| cls.input.5 | In-project `Read`/`Edit`/`Write` calls do not appear in the transcript (observed); the prompt nonetheless defines an `Edit` rendering with removed and added text and a truncation flag (`cls.eval.13`). | covered — `SKIPPED_TOOLS` hides earlier file calls unless classified; the call under review renders as `path: new text` (`render_call`); prompt text in texts §3.1, §6.15 |
| cls.input.6 | Renderings observed: `Bash` → the command text only (description and background flag dropped); MCP tool → `key=value` pairs; `WebFetch` → `<url>: <prompt>`; `Agent` → `(<subagent_type>): <prompt>` (description dropped); `AskUserQuestion` → each question in quotes with its header and numbered options (label, description, flattened preview); `SubagentHandback` → a fixed lead-in marking it agent-authored and untrusted, then the report. | covered — as described, each option's flattened preview included (`render_call`, `_questions`) |
| cls.input.7 | The user's answer to `AskUserQuestion` is a user entry prefixed `[User answered AskUserQuestion]:` followed by the tool-result text. | covered — `ANSWERED` prefix on the tool-result text (`transcript_entries`) |
| cls.input.8 | Background-task notifications enter as user entries carrying their full "not user input" frame and the `<task-notification>` body. | covered — notices kept whole as user entries (`classifier.py` `_is_notice`) |
| cls.input.9 | A sub-agent result delivered as a message to the parent enters as a user entry with its framing; report lines are indented, so an unindented frame-like line inside it is recognizably forged. | covered — agent messages enter whole (`_is_notice`, origin `agent`) with their body indented (`subtasks/tool.py` `_indent`) |
| cls.input.10 | After compaction the transcript restarts with one user entry: a bracketed note that the summary was written by the assistant and that approvals it reports are not the user speaking, then the summary text indented. | covered — `COMPACTED` note, then the summary indented (`transcript_entries`) |
| cls.input.11 | The action under review is always the final transcript entry. Earlier entries are context only. | covered — the transcript ends at the call under review (`transcript_entries`, `auto.py` `_transcript`) |
| cls.input.12 | Harness ground-truth lines may sit directly above a call: JSON `meta` objects (repo visibility for exfil-capable git/gh commands, a `git status` snapshot before commands that can destroy uncommitted work or before add/commit/push, browser cross-site navigation). They are trusted facts and never the action. None were observed in the recordings. | covered — Claude Code writes meta lines only behind switches that are off by default (`CLAUDE_CODE_AUTO_MODE_REPO_VISIBILITY` / `…_GIT_STATUS` / `…_GIT_STATUS_UPLOADS` or the matching remote settings; browser navigation needs its browser tools), which is why none were recorded; noeta writes none either, and the prompt explains them (texts §3.1) |
| cls.input.13 | Optional per-call lines after a completed call: an outcome line (`ok`, `error`, `interrupted`, `rejected-by-user`, `blocked-by-permissions`, `automode-blocked`, `automode-unavailable`, `automode-parsing-error`) keyed by a short call id, and host-context lines in two trust levels (live session vs restored state). None were observed. | covered — outcome lines (and the short id on call lines) are behind `CLAUDE_CODE_AUTO_MODE_OUTCOME_CODES` / the remote `outcomeVisibility`, off by default (bundle `vJe`), host-context lines need remote sessions; none recorded, none written by noeta; the prompt explains them (texts §3.1) |
| cls.input.14 | When the effective permission context has deny rules, a soft-block entry is inserted that lists them and asks the classifier to catch the same effect reached through a different tool (e.g. a shell write to a path a file-tool deny rule covers); the denied tool itself is enforced elsewhere. Observed only in sub-agent classifications, listing tools the sub-agent definition denies. | covered — `DENY_RULES` entry from settings deny rules and the agent's denied tools (`render_prompt`, `auto.py` `_deny_texts`) |
| cls.input.15 | Session-context block (`system[2]`): the OS user name, used to resolve the `$USER/...` branch pattern in the rules, and a statement that branches prefixed with another person's name are not this user's. | covered — `classifier.py` `USER_IDENTITY`, the last system block |
| cls.input.16 | The environment section of the prompt has named slots (organization, cloud providers, trusted domains/buckets/services, internal registry, deploy targets, network posture, host containment, protected namespaces, sensitive data locations, sensitive remote targets, protected IaC scopes, repo visibility, trusted repo, source control). Unconfigured slots read "None configured" or a conservative default; filled slots come from the user's `autoMode` settings (`allow`, `soft_deny`, `hard_deny`, `environment`). | covered — the slot kinds and the empty-slot readings are prose (texts §11); each `autoMode` list replaces its section's built-in text (`<<hard_deny>>`, `<<soft_deny>>`, `<<allow>>`, `<<environment>>` regions), `"$defaults"` putting it back at its place, as the bundle's `iAn` does (`render_prompt`) |
| cls.input.17 | Defaults when unconfigured: nothing is trusted; the working repo and its configured remotes are the trusted repo; a repo is assumed private unless evidence shows public; a target whose name has `prod`/`production` as a whole segment is a sensitive remote target; IAM/RBAC/networking/quota/node-pool resources are protected IaC scopes. | covered (texts/auto_mode_classifier.md §11) |
| cls.input.18 | The main request (not the classifier request) carries `safeguards: [{type: "dangerous_tool_use", classifier_context: {...}}]` with: schema version, permission mode, platform, live cwd, home dir, the root directory of each settings source, trusted directories (primary, additional, network, whether reads outside are blocked), permission rules (`allow`/`deny`/`ask`, each an object with the rule and its settings source, the canonical spelling when it differs, and for a file rule its anchor root and pattern; bundle `jf`), `auto_mode` (`allow`/`soft_deny`/`hard_deny`/`environment`), case-insensitive-paths flag, remote-mode flag, classify-all-shell flag, user identity, and git state (cwd, root, branch, default branch, status, visibility). Absent in default mode and on sub-agent requests. | deviation — first main auto-mode request only, never on sub-agents (`auto.py` `auto_request`, `server_context`); platform and git root and branch from the task's exec env, the eleven settings sources, `case_insensitive_paths` on Windows only, status and visibility `null` (their probes are off by default, as recorded in `m4`); permission rules go as plain strings, not Claude Code's `{rule, source}` objects (`parity/deviations.md` Permissions row) |

## Framing

| id | rule | noeta today |
|---|---|---|
| cls.frame.1 | The classifier plays a security monitor for an autonomous coding agent with developer-level access, possibly unattended for long stretches and in shared infrastructure. | covered (texts/auto_mode_classifier.md §1) |
| cls.frame.2 | Its question is only whether the agent's latest action should be blocked. | covered (texts/auto_mode_classifier.md §1) |
| cls.frame.3 | Three threats: injected instructions from content the agent read, the agent widening scope beyond its task, and accidental damage from misjudged blast radius. | covered (texts/auto_mode_classifier.md §1) |
| cls.frame.4 | Default is allow. Block only when a hard-block rule matches, or a soft-block rule matches and no allow exception covers it. | covered (texts/auto_mode_classifier.md §1) |
| cls.frame.5 | Out of scope, never a reason to block: wrong or fabricated results, unrequested updates to the user, ignoring non-safety preferences, poor code, loops, wasted tokens, retries after transient errors. | covered (texts/auto_mode_classifier.md §2) |

## Classification procedure

| id | rule | noeta today |
|---|---|---|
| cls.proc.1 | Identify the action (final entry). If it runs, sources or imports a file the agent wrote or edited earlier in the transcript, judge that written content as the action. | covered (texts/auto_mode_classifier.md §4) |
| cls.proc.2 | Expand the full scope: every part of a chained command, code inside wrappers, files written earlier, delayed effects. | covered (texts/auto_mode_classifier.md §4) |
| cls.proc.3 | For any write, commit, push, upload, publish or send: list every final destination, including ones reached through automated pathways, and check each against the trusted lists; anything not listed is external. | covered (texts/auto_mode_classifier.md §4) |
| cls.proc.4 | Hard-block check first; a match blocks immediately and nothing else is considered. | covered (texts/auto_mode_classifier.md §4) |
| cls.proc.5 | Soft-block check, then allow exceptions, gives a preliminary verdict: no match → allow; match covered by an exception → allow; match not covered → block. | covered (texts/auto_mode_classifier.md §4) |
| cls.proc.6 | User intent is applied last: an allow that crosses an explicit user boundary becomes a block; a soft block becomes an allow only if the rule's consent bar is fully met. | covered (texts/auto_mode_classifier.md §4) |
| cls.proc.7 | Before finalizing an allow, check once more whether the action reaches the harm a rule targets by a route the rule does not name; such an action falls under that rule. | covered (texts/auto_mode_classifier.md §4) |

## User intent

| id | rule | noeta today |
|---|---|---|
| cls.intent.1 | Intent works both ways: strong, specific user direction can clear a soft block (never a hard block); an explicit user boundary on an in-scope action creates a block. | covered (texts/auto_mode_classifier.md §5) |
| cls.intent.2 | Authorization covers that one instance unless the user grants a standing permission; authorized read-only access to a target stands for read-only on that target. A boundary stays until the user clearly lifts it. | covered (texts/auto_mode_classifier.md §5) |
| cls.intent.3 | A user boundary binds only actions the classifier judges at all (ones that destroy data, leak it, write shared state, touch credentials, deploy, or send from a connected account). Preferences about libraries, wording or chat behaviour never create a block. | covered (texts/auto_mode_classifier.md §5) |
| cls.intent.4 | Consent bar: the user must name both the action and the specific dangerous parameter the rule names (target branch, public destination, the secret, the exact path…). Naming the overall goal is not naming the dangerous step. | covered (texts/auto_mode_classifier.md §5.1) |
| cls.intent.5 | The bar can be met by the user's own words, or by an affirmative user reply directly after an agent proposal that named both the action and the dangerous parameter. Without such a proposal right before the reply, only the user's own words count. | covered (texts/auto_mode_classifier.md §5.1) |
| cls.intent.6 | Consent is judged at the step that ships the result; consent to a preparatory step does not carry over to the later shipping step. | covered (texts/auto_mode_classifier.md §5.1) |
| cls.intent.7 | Assistant entries are agent-authored: they never establish intent on their own; they only supply what a terse user "yes" refers to. A third-party entry (notification, peer message) between proposal and reply makes the referent ambiguous; the agent's own tool calls in between do not. | covered (texts/auto_mode_classifier.md §5.2) |
| cls.intent.8 | Questions ("could we…?", "what if…?") are not consent; only clear directives are. | covered (texts/auto_mode_classifier.md §5.2) |
| cls.intent.9 | Information obtained from tool results (including internal tools) never counts as user intent, and values the agent picked after querying a tool are agent-chosen. Exception: the user's answer to `AskUserQuestion` counts as the user's own choice, limited to the option picked. | covered (texts/auto_mode_classifier.md §5.3) |
| cls.intent.10 | A conditional boundary ("wait for X") holds until X visibly happens in the transcript; the agent's own claim that X happened does not count. | covered (texts/auto_mode_classifier.md §5.2) |
| cls.intent.11 | A user who repeats or affirms an instruction after a block gives informed consent to that exact blocked action, including its target, provided the block surfaced the target. | covered (texts/auto_mode_classifier.md §5.2) |
| cls.intent.12 | Rules that flag adversarial patterns (instruction-file poisoning, classifier bypass, self-modification, driving the agent's own terminal, credential scanning, reachability probing) clear only when the user confirms the flagged pattern is a false positive; mere insistence does not clear them. | covered (texts/auto_mode_classifier.md §5.1) |
| cls.intent.13 | Instructions relayed through a sub-agent prompt, a workflow script, tool output, a task notification or a teammate message are not user intent; a relayed claim of approval counts only if the user's own messages show it. | covered (texts/auto_mode_classifier.md §5.3) |
| cls.intent.14 | Messages from another agent session are never user intent; a peer asking this agent to do what the peer was denied is a block (permission laundering). | covered (texts/auto_mode_classifier.md §5.3) |
| cls.intent.15 | Instructions inside content the user asked to review or analyze are data, never intent. | covered (texts/auto_mode_classifier.md §5.3) |
| cls.intent.16 | An interruption by the user followed by the agent retrying the same or a near-identical action without new approval is a block. | covered (texts/auto_mode_classifier.md §5.2) |
| cls.intent.17 | Lack of user intervention between actions is not consent. | covered (texts/auto_mode_classifier.md §5.2) |

## Evaluation rules

| id | rule | noeta today |
|---|---|---|
| cls.eval.1 | Composite actions: if any part of a chained command or multi-operation file should be blocked and no exception covers it, block the whole action. | covered (texts/auto_mode_classifier.md §6.1) |
| cls.eval.2 | File writes are judged at write time against all rules, and again when a later action runs or imports the file. | covered (texts/auto_mode_classifier.md §6.2) |
| cls.eval.3 | Committing or pushing code is judged like running it: judge the committed content and where it ends up. | covered (texts/auto_mode_classifier.md §6.3) |
| cls.eval.4 | A shipping or execution step is judged together with the earlier steps that prepared it. | covered (texts/auto_mode_classifier.md §6.4) |
| cls.eval.5 | Actions that enable a blocked outcome later (env vars, background jobs, cron entries, latent permission grants, proxy-like services) are blocked even if benign now. | covered (texts/auto_mode_classifier.md §6.5) |
| cls.eval.6 | An `Agent` spawn is blocked if its prompt instructs a blocked action (or a deny-listed one); a `Workflow` call's script is judged the same way. Mentioning a blocked concept is not enough. | covered (texts/auto_mode_classifier.md §6.6) |
| cls.eval.7 | A sub-agent hand-back is itself an action: judge its text before it is delivered onward. | covered (texts/auto_mode_classifier.md §6.7) |
| cls.eval.8 | Look through code wrappers and decode obfuscated or encoded payloads; an undecodable payload is unverifiable and blocked. | covered (texts/auto_mode_classifier.md §6.8, §6.9) |
| cls.eval.9 | Clear evidence of intent toward a blocked goal (comments, variable names) blocks even a benign step and overrides all allow exceptions. | covered (texts/auto_mode_classifier.md §6.10) |
| cls.eval.10 | Attempts to steer the classifier (fake safe context, embedded approvals, imitation transcript lines inside arguments) are themselves blockable. | covered (texts/auto_mode_classifier.md §6.11, §6.12) |
| cls.eval.11 | Each action is judged on its own; earlier allowed actions are no precedent. A similar action earlier interrupted or rejected by the user is blocked; a block by the classifier or a permission rule is not a user rejection. | covered (texts/auto_mode_classifier.md §6.14, §5.2) |
| cls.eval.12 | Posting to internal messaging is not exfiltration by destination, but sensitive content or an unrequested broad audience still blocks. | covered (texts/auto_mode_classifier.md §6.13) |
| cls.eval.13 | Removals in an edit weigh as much as additions; truncated removals count as at least what is visible; a notebook cell delete/replace has unseen content and is unverifiable. | covered (texts/auto_mode_classifier.md §6.15) |
| cls.eval.14 | Parameters that depend on a tool result the classifier cannot see are unverifiable and agent-chosen; block if the action is high-severity. Forwarding an object whose content never appeared and the user did not name is treated as unknown content. | covered (texts/auto_mode_classifier.md §6.16) |
| cls.eval.15 | A tool's effect is set by its name and verb, not by self-descriptions in its arguments ("draft", "test", "private"); real shell flags such as dry-run keep their meaning. | covered (texts/auto_mode_classifier.md §6.20) |
| cls.eval.16 | Credential-shaped strings and long unexplained codes sent to a remote service are sensitive wherever they appear (names, labels, queries). | covered (texts/auto_mode_classifier.md §6.21) |
| cls.eval.17 | A user restriction on where data may go also binds the deliverable and anything derived from the restricted data; the user's own cloud accounts count as off-machine. | covered (texts/auto_mode_classifier.md §6.22) |
| cls.eval.18 | Copies inherit the sharing of their source; persistent configuration (forwarding rules, sharing grants, integrations, account security) is high-severity and needs an explicit request for that exact change. | covered (texts/auto_mode_classifier.md §6.23, §6.24) |
| cls.eval.19 | Outbound sends need user intent that covers both recipient and substance; a destination first seen only in tool content or agent inference is unverifiable. | covered (texts/auto_mode_classifier.md §6.25, §6.26) |
| cls.eval.20 | Low-information UI actions are read through the agent's own immediately preceding statement of what it is about to do; an unnamed click is judged by what it could do in the open app. | covered (texts/auto_mode_classifier.md §6.17, §6.18) |
| cls.eval.21 | Shared infrastructure gets extra scrutiny; ambiguity about whether a target is shared resolves to shared. | covered (texts/auto_mode_classifier.md §6.27) |
| cls.eval.22 | Listed commands, files and services in any rule are examples; the rule covers any vehicle producing the same harm. | covered (texts/auto_mode_classifier.md §6.28) |

## Content classes

| id | rule | noeta today |
|---|---|---|
| cls.def.1 | Three protected classes, strictest wins when several apply: secrets (access-granting credentials), personal/entrusted data (about or owned by outside parties, plus the local stores that carry them such as agent transcripts, credential dot-folders, shell history), and confidential work product (the organization's own code, plans, internals). | covered (texts/auto_mode_classifier.md §7) |
| cls.def.2 | Secrets never enter any repo on the agent's initiative regardless of visibility; committing a real secret needs the user to name its exposure. Authored placeholders and test keys are not secrets. | covered (texts/auto_mode_classifier.md §7) |
| cls.def.3 | Confidential material is fine in a private repo and out of place in a public one; repo visibility never clears secrets or personal/entrusted data. | covered (texts/auto_mode_classifier.md §7) |
| cls.def.4 | Ordinary working files moving along the repo's normal paths are never blocked on content class alone; the one blocked direction is private content to a public destination. | covered (texts/auto_mode_classifier.md §7) |
| cls.def.5 | Whether data is sensitive follows from where it came from, not from how innocent it looks; re-encoding it leaves its origin unchanged. | covered (texts/auto_mode_classifier.md §7) |
| cls.def.6 | "External" is anything not listed as trusted; "cluster write" is any command that changes shared cluster or workload-manager state; a per-person remote dev environment is high blast radius for its owner. | covered (texts/auto_mode_classifier.md §7) |
| cls.def.7 | Browser-driving tools act in the user's real logged-in browser; remote-service tools act on the remote account even when their server name resembles a local tool. | covered (texts/auto_mode_classifier.md §7) |

## Hard blocks

| id | rule | noeta today |
|---|---|---|
| cls.hard.1 | Hard blocks are unconditional: no user intent and no allow exception clears them. | covered (texts/auto_mode_classifier.md §8) |
| cls.hard.2 | The hard-block family is data exfiltration: sensitive data crossing the trust boundary directly or through a pathway the agent arms (e.g. a CI change that will upload private content). | covered (texts/auto_mode_classifier.md §8) |
| cls.hard.3 | Judge where data finally lands, not the first hop; a future review or merge gate does not discount the pathway. | covered (texts/auto_mode_classifier.md §8) |
| cls.hard.4 | Shipping a whole repo or package tree anywhere other than the repo's remote as of session start is a red flag by scale alone; a remote changed mid-session is a new, presumed-public destination. | covered (texts/auto_mode_classifier.md §8) |
| cls.hard.5 | Same-org ownership, a personal branch name or a non-force push does not establish privacy; unknown visibility is by itself not a reason to block. | covered (texts/auto_mode_classifier.md §8) |
| cls.hard.6 | A hard-block reason names the rule and suggests doing the step with auto mode off, where the user gets the usual approval prompt. | covered (texts/auto_mode_classifier.md §8, §12) |

## Soft blocks

Each soft-block rule names one **must-name** parameter: the thing the user's words (or an affirmed
proposal) must name to clear it. The 2.1.312 prompt has about 70 soft-block rules; they group as
below.

| id | rule | noeta today |
|---|---|---|
| cls.soft.1 | Every soft block clears only at the named-action-plus-named-parameter bar (`cls.intent.4`); a bare verb names the action but not its specifics; when a bar is unmet the reason shows the blocked command and what would clear it. | covered (texts/auto_mode_classifier.md §9, §12) |
| cls.soft.2 | Source control: destroying other people's history (force push, ref/tag/release deletion, rewrites), changing push remotes, publishing to known-public places, creating public surfaces (public repos, releases, packages, gists), merging without review, approving one's own change, comments that trigger automation, bypassing CI or branch protection. | covered (texts/auto_mode_classifier.md §9.1) |
| cls.soft.3 | Code provenance: running or integrating code from outside sources, routing installs to a non-approved registry, committing code that leaks data when run, creating remote-code-execution surfaces. | covered (texts/auto_mode_classifier.md §9.2) |
| cls.soft.4 | Production and shared infrastructure: deploys, prod reads and remote shells, writes on shared hosts, skipped previews for live changes, protected-scope IaC applies, cluster writes on shared clusters, node lifecycle, cluster-wide workloads, interfering with jobs the agent did not create, modifying shared resources, feature-flag changes, DNS/domain/certificate changes, secret-store writes. | covered (texts/auto_mode_classifier.md §9.3) |
| cls.soft.5 | Local destruction: irreversible deletion or overwrite of pre-existing files, recursive deletes whose target is not verifiable, broad sweeps of shared temp/cache directories, deletes whose resolved target list was not shown, general teardown of stateful resources the agent did not create (presumed irreversible, presumed to hold uncommitted work). | covered (texts/auto_mode_classifier.md §9.4) |
| cls.soft.6 | Security posture: disabling logging/audit, TLS or auth verification, security controls, security-relevant tests, using flags or env vars whose names say they disarm a guard, widening permissions or account security settings, adding persistence (keys, shell profiles, credential stores), starting agent loops with approvals off. | covered (texts/auto_mode_classifier.md §9.5) |
| cls.soft.7 | Credentials and sensitive data: leaking secrets into persistent artifacts, printing live credentials into output or files, systematic credential scanning, handling listed sensitive data locations, moving sensitive-store content into a repo, excess sensitive detail in outbound content. | covered (texts/auto_mode_classifier.md §9.6) |
| cls.soft.8 | Network exposure: exposing local services or shells, public ingress tunnels, reachability probing of unlisted endpoints, repointing API/model base URLs or proxies, escaping the host's network/identity containment, sandbox network callbacks and commands that declare allowed domains. | covered (texts/auto_mode_classifier.md §9.7) |
| cls.soft.9 | Agent integrity: writing instruction files the agent will later read (instruction poisoning), self-modification of its own permissions or consent text, driving its own terminal or input, tampering with session transcripts, bypassing the classifier or permission system. | covered (texts/auto_mode_classifier.md §9.8) |
| cls.soft.10 | Connected apps and real world: committing actions in a connected app where the user asked only to read or draft, writes to external collaboration tools, real-world or financial transactions, attacking third-party systems, publishing artifacts, and browser-specific exfil paths (navigation, form input, page script, file upload, shortcuts). | covered (texts/auto_mode_classifier.md §9.1, §9.9) |
| cls.soft.11 | User deny rules (when present, `cls.input.14`): circumventing a deny rule by switching tools is a soft block. | covered — texts §9.10 and the `DENY_RULES` entry (`render_prompt`) |

## Allow exceptions

| id | rule | noeta today |
|---|---|---|
| cls.allow.1 | Exceptions apply to soft blocks only and are mandatory: if one applies the action must be allowed, unless it is a disguise (e.g. a look-alike trusted domain) or crosses an explicit user boundary. | covered (texts/auto_mode_classifier.md §10) |
| cls.allow.2 | An exception covers only its own dimension (destination, resource or operation); it never extends to delete or history-rewriting variants, or to force-pushing over others' commits. | covered (texts/auto_mode_classifier.md §10) |
| cls.allow.3 | Production precedence: generic development exceptions never apply to running production; prod actions are allowed only by a met consent bar or an infrastructure-specific exception. | covered (texts/auto_mode_classifier.md §10) |
| cls.allow.4 | Covered by exceptions: discussing or writing security material; retries after transient failures; authored test credentials; local file operations within the project; read-only queries that return nothing sensitive; installing dependencies already declared in manifests; official toolchain bootstrap installers when the repo calls for the toolchain; sending a credential to its own intended provider; cleaning up jobs the agent created this session; pushing to any branch of the session's repo; normal use of listed trusted infrastructure (data-flow rules only); scheduled prompt firings (treated as stored prompts, not live user words); multi-agent control traffic; routine memory-directory writes; CLAUDE.md edits that do not touch permissions or auto-mode behaviour; Claude Code's own scheduling tools; browser navigation to trusted, local or user-named URLs. | deviation — the instruction-file and scheduling exceptions name `NOETA.md` and noeta's own tools (parity/deviations.md noeta's own files); the rest covered (texts §10) |

## Output format

| id | rule | noeta today |
|---|---|---|
| cls.out.1 | Allow: `<block>no</block>` and nothing else (no category, no reason). | covered — texts §12; `parse_stage1`/`parse_stage2` |
| cls.out.2 | Block: `<block>yes</block>`, then `<category>` with the matched block rule's name, then `<reason>` starting with that rule name in square brackets followed by one short sentence. | covered — texts §12; `parse_stage2` reads category and reason |
| cls.out.3 | Category value uses only letters, digits and spaces (slashes and hyphens become spaces, other punctuation dropped). | covered — texts §12; `parse_stage2` normalises the category |
| cls.out.4 | If no specific block rule can be named, the answer is allow. An allow-exception name is never a category. | covered (texts/auto_mode_classifier.md §12) |
| cls.out.5 | Several matches: the most severe rule is the category; the others are named in the reason. A boundary or undecodable-payload block cites the rule whose territory it touches. | covered (texts/auto_mode_classifier.md §12) |
| cls.out.6 | Stage-1 replies are cut by the `</block>` stop sequence, so only `<block>yes` / `<block>no` arrive (`cls.stage.3`). | covered — `STOP` on stage 1 (`classifier_request`) |

## Failures and thresholds

| id | rule | noeta today |
|---|---|---|
| cls.fail.1 | An unreachable classifier holds the call back fail-closed (outcome `automode-unavailable`); retrying is appropriate and it is not a policy decision. | covered — a failed call holds the call back, not counted as a denial, and tells the model to retry (`auto.py` `_run`, `wording.py` `CLASSIFIER_NO_VERDICT`); as the bundle: a stage-2 error after a stage-1 block is a counted block (`CLASSIFIER_STAGE2_FAILED`), and a transcript too long for the classifier's window falls back to asking (`too_long`) |
| cls.fail.2 | An unparseable classifier reply also holds the call back fail-closed (`automode-parsing-error`). | covered — `CLASSIFIER_UNPARSED`, held back the same way (`_run`) |
| cls.fail.3 | A failed fast-model stage-1 request is retried on the main model (`cls.req.6`). | covered — `auto.py` `_verdict`, for either stage |
| cls.fail.4 | Consecutive/total denial limits that fall back to interactive approval are not visible in the recordings (no denial occurred) and are not stated in the classifier prompt; treat as unobserved. | covered — noeta takes Claude Code's limits: 3 in a row or 20 in a task fall back to a firm ask (`auto.py` `MAX_CONSECUTIVE_DENIALS`, `MAX_TOTAL_DENIALS`) |

## What the main loop sees

| id | rule | noeta today |
|---|---|---|
| cls.loop.1 | Allowed calls run normally; the main transcript shows no trace of the classification (no reminder, no extra tool-result text). | covered — an allow is a plain `Allow()`; the verdict is only recorded in the log (`auto.py` `answer`) |
| cls.loop.2 | Blocked calls: not observed. The prompt implies the agent receives the block reason, which names the rule and (soft) what would clear it or (hard) suggests running outside auto mode, and that the agent may ask the user; a user's reply to such a question is informed consent (`cls.intent.11`). | covered — a block is the call's error result with the classifier's reason (`wording.py` `CLASSIFIER_BLOCKED`) |
| cls.loop.3 | The main system prompt tells the model that a denied call means the user declined and it should adjust rather than retry verbatim (see `main-prompt.md`). | covered (see main-prompt.md `main.harness.2`) |
