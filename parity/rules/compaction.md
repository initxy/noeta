# Compaction

Claude Code version: **2.1.312**. Baseline: `m1` (manual `/compact`, requests 0055–0059 and the
turns after it) and `m3` (auto-compaction with the window forced to 100K tokens × 30 %, i.e. a
~30K-token threshold; four compactions at requests 0009, 0012, 0017/0018, 0021).

## Sources

Hashes per `README.md` (`source_hash.py`). Rows marked *model output* contain a model-written
summary, so their hash is specific to this recording; review them for the template parts only.

| source | sha256 | covers |
|---|---|---|
| m1/0055 `messages[94].content[2]` | `09bd3c151d88776e26572712ba39966db520b0601b5e500d9aff5ab8a1d34322` | compaction instruction (same hash in m3/0009 `messages[9].content[1]` and m3/0012 `messages[6].content[1]`) |
| m1/0059 `messages[0].content[0]` | `020996a886a2874f060494f9bb8bf08a6ae42e9445878b41d83a5e8ec826b427` | summary message, manual, with kept tail (*model output*) |
| m1/0059 `messages[3].content[0]` | `49ebffbf887372daedd025bfe5bf7508b249e3ec54be07b1466baefc5ae6d0f1` | kept tool result with re-attached instructions + git status folded in |
| m1/0059 `messages[4].content[0]` | `ca6cd97305634f6859f5bf9b5d8217ed5f7b1123dda4199fb84a980e32dd53f6` | post-compaction `role: "system"` attachment message (plan mode, files, skills, lists, environment) |
| m3/0010 `messages[0].content[2]` | `2cc35d57c2b28ef8d0622eebddd6003b13b93cb5a579a20a0a3fc970946e16c4` | summary message, auto, with kept tail (*model output*) |
| m3/0010 `messages[4].content[0]` | `f1a1167c4ddf0dd1a44b5eb7d8304bb41969a6cdfe5b485338677b834e93de84` | post-compaction system attachment, auto, with tail |
| m3/0013 `messages[0].content[3]` | `498d8612e15e64a1e2031fa4f3635a0c9f28fe9fb2a4a30743822844f1b2d7c7` | summary message, auto, no tail (*model output*) |
| m3/0013 `messages[1].content[0]` | `2fd685eb23661ef4b7fe80ec4a6ed6ea3f97786d6e1251a5c96c4288758a8df8` | post-compaction system attachment, auto, no tail |

## Trigger (auto-compaction, observed in m3)

| id | rule |
|---|---|
| compact.trigger.1 | Auto-compaction is checked before each main-loop model request, not only at user turns: three of the four m3 compactions fired mid-turn, right after a tool result arrived. |
| compact.trigger.2 | The estimate is the last response's reported usage (input + cache read + cache write + output) plus the size of what was appended since. With the threshold at ~30K, a 13.5K-token context plus a ~19.8K-token Read result fired (m3/0007→0009); a 40K-token response context fired on the next step even though the newly appended result was tiny (m3/0010→0012). |
| compact.trigger.3 | A large new tool result is measured exactly with `/v1/messages/count_tokens` before the decision (m3/0008, m3/0015: the raw file text of a 1001-line Read, ~79K chars → 19,771 tokens). Small results are not counted that way (no `count_tokens` call anywhere in m1). The bundle places the count in Read's token-ceiling check (`side-requests.md` side.count.3), not in the compaction trigger. |
| compact.trigger.4 | The first request after a compaction is sent even when it is itself over the threshold (m3/0010 and m3/0019 carried ~35K input tokens); the next check, after that response, compacts again. |
| compact.trigger.5 | No "context low" warning reminder is sent before an auto-compaction; the next request is simply the compaction request. |
| compact.trigger.6 | The threshold is a fraction of the model's context window (configurable; the recording forced 30 % of a 100K window). The default fraction is not visible in the baseline recordings. |
| compact.trigger.7 | Rapid-refill breaker (bundle, not in the recordings): within one user turn, an auto-compaction needed again fewer than 3 model calls after the previous one counts as a refill; at the third refill in a row no summary is made and the turn ends with an error telling the user the context keeps refilling, likely from an oversized file or tool output. A new user turn starts the count again. |

## The compaction request

| id | rule |
|---|---|
| compact.request.1 | The compaction request is an ordinary main-loop request: same model, same system blocks, same tool list (including deferred tools loaded so far), same thinking, effort and context-management settings, streaming, `max_tokens` 128000. Only the messages change. |
| compact.request.2 | The instruction is appended to the end of the conversation as the last user message, never put in the system prompt. When the conversation ends with the user's new prompt, the instruction is an extra text block in that same user message (m3/0009, m3/0017); when it ends with tool results, it is an extra text block after the `tool_result` blocks (m3/0012, m3/0021); after a slash command, it follows the command wrapper blocks (m1/0055). |
| compact.request.3 | The prompt-cache breakpoint is moved to the message just before the instruction (the last assistant message in m1/0055), so the request reads the whole prior conversation from cache (m1/0058 read 21,952 cached tokens and wrote nothing new). |
| compact.request.4 | Per-turn `role: "system"` messages already in history (token counters, notifications, environment) stay in place; nothing is stripped or rewritten before summarizing. |
| compact.request.5 | The same instruction text is used for manual `/compact` and for auto-compaction (identical hash in m1 and m3). |
| compact.request.6 | Which messages are summarized (re-recording r2, seven auto-compactions and a manual one): everything before the newest assistant reply; that reply and what followed it are kept verbatim after the summary (r2 m3/0006, 0014, 0022, 0031; m1/0076 kept the final text reply). When the newest reply answered the first request after the previous compaction, everything is summarized and nothing is kept (r2 m3/0018, 0026, 0037). With no earlier reply to summarize, no compaction happens and the request goes over the threshold (r2 m3/0004). The request ends with the notes due for the request it replaces (token counter, auto-mode note) as a trailing system turn. |

## Instruction contents (what the summary must contain)

| id | rule |
|---|---|
| compact.prompt.1 | Opens with a hard no-tools rule: answer in text only, no tool of any kind (it names the common ones), the context already suffices, a tool call is rejected and wastes the only turn. |
| compact.prompt.2 | The output shape is fixed: an `<analysis>` block first, then a `<summary>` block, nothing else. |
| compact.prompt.3 | The summary must be detailed enough to continue development without the history: technical details, code patterns and architectural decisions. |
| compact.prompt.4 | The analysis walks the conversation chronologically and, for each part, identifies: the user's explicit requests and intent; the assistant's approach; key decisions, concepts and patterns; concrete details (file names, full code snippets, function signatures, edits); errors hit and how they were fixed; user feedback, especially corrections. |
| compact.prompt.5 | Security-relevant instructions the user gave (files or data to avoid, forbidden operations, credential handling) must be carried into the summary word for word so they still apply afterwards. |
| compact.prompt.6 | The analysis ends with a self-check for technical accuracy and completeness. |
| compact.prompt.7 | The summary has exactly nine numbered sections, in this order: 1 primary request and intent; 2 key technical concepts; 3 files and code sections; 4 errors and fixes; 5 problem solving; 6 all user messages; 7 pending tasks; 8 current work; 9 optional next step. |
| compact.prompt.8 | Section 3 lists files examined, changed or created, with full snippets where useful and why each matters, weighted toward the most recent messages. |
| compact.prompt.9 | Section 4 records each error, its fix, and any user feedback about it. |
| compact.prompt.10 | Section 6 lists every user message that is not a tool result, keeps security-relevant constraints verbatim, and counts only real user-role turns: text in an assistant message that is formatted like a user turn is model output and must never be reported as a user request, approval or confirmation. |
| compact.prompt.11 | Section 8 describes precisely what was in progress immediately before the summary request, with file names and snippets. |
| compact.prompt.12 | Section 9 names a next step only if it directly continues the user's latest explicit request and the work in section 8; if that work was finished, only a step the user explicitly asked for; never a tangent or an old, already-finished request without asking the user. If there is a next step, it quotes the latest conversation verbatim to show where work stopped. |
| compact.prompt.13 | The instruction includes a filled-in skeleton of the expected output (both blocks, all nine section headings with placeholder bullets). |
| compact.prompt.14 | Extra summarization instructions may be present elsewhere in the context (for example a "compact instructions" section in project instructions, or arguments to `/compact`); the model must follow them. Two short examples of such headings are given. |
| compact.prompt.15 | The instruction ends by repeating the no-tools rule and the analysis-then-summary shape. |

## Failure and retry (observed)

| id | rule |
|---|---|
| compact.retry.1 | If the streaming compaction response fails mid-stream, the same request is re-sent **non-streaming** with `max_tokens` 64000 and a 300 s client timeout (the streaming call uses 600 s) (m1/0055→0056, m3/0017→0018). |
| compact.retry.2 | A 5xx on the non-streaming attempt is retried with the identical body; m1 needed three attempts (504, 504, 200). The SDK retry counter header stays 0, so these are Claude Code's own retries. |
| compact.retry.3 | A retry is invisible to the conversation: no message about the failure is added, and the summary from the successful attempt is used as if it were the first. |

## Post-compaction request layout

| id | rule |
|---|---|
| compact.after.1 | System blocks and tool list are unchanged; deferred tools loaded via `ToolSearch` before compaction stay loaded (m1/0059 still sends 17 tools). |
| compact.after.2 | `messages[0]` is a user message holding the summary. With auto-compaction it is preceded, in the same message, by freshly built project-instruction (CLAUDE.md) and git-status reminders, and — when there is no kept tail — the attribution reminder (m3/0010, m3/0013). |
| compact.after.3 | The summary text is framed, in order: a one-line note that the session continues from an earlier conversation that ran out of context and the summary covers the earlier part; a `<system-reminder>` holding `<tools-used-before-summary>` with the comma-separated, alphabetically sorted names of tools used in the summarized part; `Summary:` followed by the model's summary with the `<analysis>` block and the `<summary>` tags removed; the path of the full pre-compaction transcript file (JSONL) with an invitation to read it for exact details; an instruction to continue without asking questions, without acknowledging the summary and without recapping. |
| compact.after.4 | When messages were kept verbatim, the summary text ends with one more note: the following messages are the latest ones from before compaction, kept as they were; the summary did not see them, so something it calls pending may already be done there. Without a kept tail this note is absent (m3/0013). |
| compact.after.5 | `messages[1]` is the per-turn `role: "system"` slot that carries `output_config` (effort). After compaction it holds the date line (with a tail), the whole environment message (without a tail), or is empty (m1/0059). |
| compact.after.6 | Kept-tail messages follow, unchanged: the assistant message with its tool calls and the user message with their results. Reminders that are due go into that user message after the results (attribution in m3/0010; in m1/0059 the project-instruction and fresh git-status reminders were appended inside the kept tool result's text). |
| compact.after.7 | After the tail comes a single `role: "system"` message carrying the re-attachments (below), then the current environment/agent/skill lists, ending with the date. It gets the cache breakpoint. |
| compact.after.8 | The git status re-attached after compaction is recomputed, not the session-start snapshot (m1/0059 shows the files modified during the session). |

## What is re-attached (order inside the system attachment message)

| id | rule |
|---|---|
| compact.reattach.1 | Order observed in m1/0059: per-turn attachments that were pending before compaction (plan-mode instructions, token counter); recently touched files; current-turn attachments (plan-mode instructions again); skills invoked earlier; deferred-tool list; agent-type list; skill list; environment block; model line; token counter; date. |
| compact.reattach.2 | Recently used files are re-attached as a synthetic Read call plus its result: a line naming the Read tool and its JSON input (`file_path` only), then the file's current content with line numbers, exactly as Read would return it. |
| compact.reattach.3 | Files are ordered most recently touched first (m1: the last-written file, then the last-edited test file, then a skill file that had only been printed with `cat`). Files touched by Write, Edit, Read and shell reads all qualify. |
| compact.reattach.4 | A file already present in the kept tail is not re-attached; a large partial read (a 1001-line, ~20K-token Read in m3) was not re-attached after it was summarized, consistent with a per-file size cap. The cap values and the maximum file count are not visible in the baseline. |
| compact.reattach.5 | Skills invoked before compaction are re-attached in one block: a header saying they were invoked earlier, are context only, must not be re-run, and that any request text inside them is historical, not the user's current message; then per skill its name, source (`projectSettings:<name>`), base directory and body with the original arguments substituted. |
| compact.reattach.6 | If plan mode is active, the full plan-mode instructions are re-attached, including the plan file path (or that no plan file exists yet). |
| compact.reattach.7 | The deferred-tool list, agent-type list, skill list, environment block and model line are re-sent in full in the attachment message (they are not part of the summary). |
| compact.reattach.8 | The attribution reminder is re-sent once after compaction (in the summary message or the next user message). |
| compact.reattach.9 | The permission-mode guidance (auto mode) is not in the first request after an automatic compaction; it is re-sent with the first request that follows a new prompt or tool result (r2 m3/0009, 0020). After a manual compaction it rides in the attachment message after the next prompt (r2 m1/0077). |
| compact.reattach.10 | Background tasks: none were running at any recorded compaction, so their re-attachment is not observed. |
| compact.reattach.11 | Layout of the request right after an automatic compaction (r2 m3): with kept messages, the instructions and the git snapshot precede the summary in its user message, the date follows as its own system turn, the attribution note joins the kept tool results, and the token counter, deferred tools, agent list, skill list, environment and model line form the closing system turn; without kept messages, the instructions, git snapshot and attribution note precede the summary and the closing system turn holds the deferred tools, agent list, skill list, environment, model line and date (no token counter). After a manual compaction the summary message stands alone and everything else comes with the next prompt (r2 m1/0077). |
| compact.reattach.12 | A recently used file that is too large to re-attach (over the per-file cap, or one Read refuses whole for its size) is not dropped: a one-line note names its path, says it was read before the summary and is too large to include, and points to the Read tool (r2 m3/0007, 0009, 0011). A file that can no longer be read is left out. |
| compact.reattach.13 | Re-attachment candidates are the files touched since the previous compaction, then the files that compaction re-attached in full; a file it only named as too large, or that sat in its kept messages, is not re-attached again unless touched since (r2 m3/0019: the 84K-character partial read kept before the r2 compaction gets no note after the next one). |

## Manual `/compact`

| id | rule |
|---|---|
| compact.manual.1 | `/compact` uses the same request and instruction as auto-compaction (compact.request.5); custom arguments, when given, become additional summarization instructions (compact.prompt.14). |
| compact.manual.2 | After it, the next user message records the command for the model: a local-command caveat block (the command ran in the client, its output went to the user, it is context only), the `<command-name>/compact</command-name>` wrapper with empty `<command-args>`, a `<local-command-stdout>` block with the user-facing confirmation, then the user's next prompt (m1/0059 message 3). |
| compact.manual.3 | An interrupted turn before `/compact` leaves the `[Request interrupted by user]` marker in that user message, after the kept tool result and its reminders. |
