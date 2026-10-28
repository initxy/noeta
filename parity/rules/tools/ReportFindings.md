# ReportFindings

Claude Code 2.1.312 · baseline `m1` · rules in our words (see `../README.md`).

| source | sha256 |
|---|---|
| `m1/0001 tools[ReportFindings].description` | `59ea4187fcd52b2f67e233b092d1c7b841a6b4156468636434886fb5f9641296` |
| `m1/0001 tools[ReportFindings].input_schema` | `0d087f6c81619ada6d77e7ad3904dddcf1f12fa1b510ad491a679c31d54584e1` |

## Purpose

Hands code-review findings to the host UI as typed data. Always loaded; position 7 of 13 in
the baseline list. Never called in the recordings. noeta has no counterpart: every row is
`missing`.

## Parameters

`additionalProperties: false` at both levels; required: `findings`.

| id | name | type | required | enum / default / limits | noeta today |
|---|---|---|---|---|---|
| `tool.ReportFindings.p.level` | `level` | string | no | enum `low`, `medium`, `high`, `xhigh`, `max`; the effort the review ran at | missing |
| `tool.ReportFindings.p.findings` | `findings` | array of object | yes | maxItems 32; most severe first; empty when nothing survived verification | missing |
| `tool.ReportFindings.p.findings.file` | `findings[].file` | string | yes | repo-relative path | missing |
| `tool.ReportFindings.p.findings.line` | `findings[].line` | integer | no | 1-based anchor line (safe-integer bounds) | missing |
| `tool.ReportFindings.p.findings.summary` | `findings[].summary` | string | yes | one sentence stating the defect | missing |
| `tool.ReportFindings.p.findings.short_summary` | `findings[].short_summary` | string | no | maxLength 60; the claim only, no reason or consequence | missing |
| `tool.ReportFindings.p.findings.failure_scenario` | `findings[].failure_scenario` | string | yes | concrete input/state leading to the wrong output or crash | missing |
| `tool.ReportFindings.p.findings.category` | `findings[].category` | string | no | maxLength 40; short kebab-case kind, e.g. correctness, simplification, efficiency, test-coverage | missing |
| `tool.ReportFindings.p.findings.verdict` | `findings[].verdict` | string | no | enum `CONFIRMED`, `PLAUSIBLE`; only when a verification pass ran | missing |
| `tool.ReportFindings.p.findings.outcome` | `findings[].outcome` | string | no | enum `fixed`, `skipped`, `no_change_needed`; only when re-reporting after applying fixes | missing |

## Rules

| id | rule | noeta today |
|---|---|---|
| `tool.ReportFindings.1` | Use it only when the active code-review instructions say to report through it; otherwise follow the output format those instructions give. | missing |
| `tool.ReportFindings.2` | Call it once per review, with verified findings ranked most severe first, or an empty array when none survived. | missing |
| `tool.ReportFindings.3` | Do not also print the findings as text after reporting them. | missing |
| `tool.ReportFindings.4` | Re-report after applying fixes only when the apply instructions ask, then setting each finding's `outcome` to what actually happened. | missing |

## Result shape

| id | shape (observed) |
|---|---|
| `tool.ReportFindings.r.1` | Not observed (never called). |

## Variants

Same hashes in every session.
