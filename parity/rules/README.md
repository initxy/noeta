# Claude Code rule checklists

Claude Code version: **2.1.312**. Baseline: recording session `m1` (API-key user, default
settings, `auto` permission mode, Opus 5.5 as Claude Code believes it). Recordings live outside
the repo in `~/cc-recordings/<version>/` (see `docs/specs/2026-10-28-claude-code-parity.md`).

## What these are

Each file lists what Claude Code shows the model, broken into **atomic, testable rules**: one
behaviour, number, limit, trigger or ordering per row. They are the "what" that noeta's own
prompts, tool descriptions, reminders and side requests must cover. Tests and reviews cite a
rule by its id.

| file | covers |
|---|---|
| `main-prompt.md` | the main system prompt by section, the `role: "system"` environment message |
| `tools/<Tool>.md` | one per tool the baseline sends (including deferred tools loaded through `ToolSearch`) |
| `reminders.md` | every `<system-reminder>` and wrapper kind seen in the recordings |
| `compaction.md` | the compaction request, the post-compaction layout, auto-compaction trigger |
| `subagent.md` | the sub-agent system prompt, tool set, how results reach the parent |
| `auto-mode-classifier.md` | structure and decision rules of the auto-mode permission classifier |
| `side-requests.md` | title, plan name, WebFetch digest, prompt suggestion, `count_tokens`, quota probe |

`main-prompt.md` and each `tools/*.md` carry a **noeta today** column: `covered`, `missing` or
`contradicts`, with a short note, measured against `noeta/agents/files/main.md`,
`noeta/plugins/*/descriptions/*.md` and the tool schemas under `noeta/plugins/`. Rules that
describe a Claude Code product feature noeta deliberately does not have (see the spec's
Non-goals) are marked `n/a` with the reason.

## No-copy rule

Claude Code is closed source (© Anthropic, all rights reserved). Nothing here quotes or closely
paraphrases its sentences. Each rule is restated in our own words as a fact: what the model must
do, which parameter exists, which number applies, when something appears. Identifiers that are
part of the wire contract (tool names, parameter names, enum values, tag names such as
`<system-reminder>`, slash-command names) are kept verbatim because a client or model depends on
the exact spelling; they are names, not prose. When in doubt, describe the effect, not the
sentence. A reviewer who can reconstruct Claude Code's wording from a row should reject it.

## Ids

`<file>.<section>.<n>`, lower-case, `-` inside a part:

- `main.harness.3`, `main.env.2`
- `tool.Bash.p.timeout` (a parameter), `tool.Bash.4` (a behavioural rule), `tool.Bash.r.2` (result
  shape)
- `rem.<kind>.<n>`, `compact.<section>.<n>`, `sub.<section>.<n>`, `cls.<section>.<n>`,
  `side.<request>.<n>`

Ids are stable. A rule that Claude Code drops is kept with `retired in <version>`; a new rule
takes the next free number in its section, never a reused one. Splitting a rule keeps the old id
on the first half.

## Sources and hashes

Every file starts with a table of the source texts it covers and the sha256 of each. A source is
named `<session>/<nnnn> <selector>`, where the selector is `system[i]`,
`tools[Name].description`, `tools[Name].input_schema`, `messages[i].content[j]` or `file:<path>`
inside the recordings directory. Hashes are taken over a **normalized** text so a recording on
another machine gives the same hash for the same template: the recording's working directory,
config directory and home directory are replaced by `$CWD`, `$CFG`, `$HOME`, then every run of
digits becomes `N`; tool schemas are hashed as canonical JSON (sorted keys, no spaces).
`source_hash.py` in this directory implements exactly that:

```sh
python3 -I parity/rules/source_hash.py ~/cc-recordings/2.1.312 m1/0001 'system[2]' 'tools[Bash].description'
```

## Updating to a new Claude Code version

1. Record the same scenarios with the new version (spec, Phase 0) into
   `~/cc-recordings/<new>/`, baseline session first.
2. For every row of every Sources table, find the same source in the new recording (the request
   index may shift; match by selector and kind) and recompute its hash with `source_hash.py`.
3. Unchanged hash: the checklist stands; bump only the version line. Changed hash: diff the old
   and new source text locally, then add, amend or retire the affected rules (keeping ids
   stable), refresh the hash, and re-mark the **noeta today** column for the touched rows.
4. A source with no counterpart (a removed tool, a reminder that no longer appears) retires its
   rules. A text in the new recording that no Sources row names (a new tool, a new reminder kind)
   gets a new checklist or section.
5. Each changed rule is a work item for noeta; the rule checklist diff is the work list.
