# AskUserQuestion

Claude Code 2.1.312 · baseline `m1` · rules in our words (see `../README.md`).

| source | sha256 |
|---|---|
| `m1/0001 tools[AskUserQuestion].description` | `f2861f2bbaa8601c0767815c37956c9c33dbc90e8fea138f6164af93a279055a` |
| `m1/0001 tools[AskUserQuestion].input_schema` | `973aeb5c51c26e46ba200cedcc5329b44fb979f30b05ab56af33d92187d623b5` |

## Purpose

Asks the user 1–4 multiple-choice questions and waits for the answers. Always loaded;
position 2 of 13 in the baseline list. noeta's counterpart is `AskUserQuestion`
(`noeta/plugins/ask_user/__init__.py`, `descriptions/AskUserQuestion.md`).

## Parameters

`additionalProperties: false` at every object level; required: `questions`.

| id | name | type | required | enum / default / limits | noeta today |
|---|---|---|---|---|---|
| `tool.AskUserQuestion.p.questions` | `questions` | array of object | yes | 1–4 items | covered |
| `tool.AskUserQuestion.p.questions.question` | `questions[].question` | string | yes | the complete, specific question ending with `?`; phrased for several answers when `multiSelect` | covered |
| `tool.AskUserQuestion.p.questions.header` | `questions[].header` | string | yes | chip label, at most 12 chars (e.g. "Auth method") | covered |
| `tool.AskUserQuestion.p.questions.options` | `questions[].options` | array of object | yes | 2–4 items, distinct and mutually exclusive unless `multiSelect`; no "Other" option (added automatically) | covered |
| `tool.AskUserQuestion.p.options.label` | `options[].label` | string | yes | 1–5 words, what the user sees and picks | covered |
| `tool.AskUserQuestion.p.options.description` | `options[].description` | string | yes | what choosing it means; trade-offs | covered |
| `tool.AskUserQuestion.p.options.preview` | `options[].preview` | string | no | content shown when the option is focused (mockup, code, comparison) | covered |
| `tool.AskUserQuestion.p.questions.multiSelect` | `questions[].multiSelect` | boolean | yes | default false; true when choices are not exclusive | covered |
| `tool.AskUserQuestion.p.answers` | `answers` | object string→string | no | filled by the permission UI with the user's answers, not by the model | covered in the schema (front ends do not fill it) |
| `tool.AskUserQuestion.p.annotations` | `annotations` | object keyed by question text → `{preview?, notes?}` | no | user notes per question; `preview` = the chosen option's preview | covered in the schema (front ends do not fill it) |
| `tool.AskUserQuestion.p.metadata` | `metadata` | object `{source?}` | no | analytics only, not shown (e.g. `source: "remember"`) | covered |

## Rules

| id | rule | noeta today |
|---|---|---|
| `tool.AskUserQuestion.1` | Use only when progress depends on a choice that belongs to the user — not resolvable from the request, the code or sensible defaults. | covered |
| `tool.AskUserQuestion.2` | The user can always choose "Other" and type free text. | covered |
| `tool.AskUserQuestion.3` | `multiSelect: true` allows several answers to one question. | covered |
| `tool.AskUserQuestion.4` | A recommended option goes first and its label ends with "(Recommended)". | covered |
| `tool.AskUserQuestion.5` | Entering plan mode is done with `EnterPlanMode`, not this tool. | covered |
| `tool.AskUserQuestion.6` | In plan mode, use it to clarify requirements or pick an approach before the plan is final. | covered |
| `tool.AskUserQuestion.7` | Never use it to ask whether the plan is ready or whether to proceed, or refer to "the plan" in a question — the user cannot see the plan until `ExitPlanMode`. | covered |
| `tool.AskUserQuestion.8` | Reserve it for decisions whose answer changes what happens next; for choices with a conventional default or facts checkable in the code, pick the obvious option, say so, and continue. | covered |
| `tool.AskUserQuestion.9` | The user may see only the question dialog, not earlier tool results or text; each question must stand alone — state briefly what it depends on and name the items asked about. | covered |
| `tool.AskUserQuestion.10` | Use `preview` to compare concrete artifacts: ASCII UI mockups, code variants, diagram variants, config examples. | covered |
| `tool.AskUserQuestion.11` | A preview is rendered as markdown in a monospace box and may span lines; when any option has one, the dialog switches to a side-by-side layout (options left, preview right). | covered |
| `tool.AskUserQuestion.12` | No previews for simple preference questions; previews only for single-select questions. | covered |

## Result shape

| id | shape (observed) |
|---|---|
| `tool.AskUserQuestion.r.1` | A plain string (m1/0047): a lead saying the questions were answered, then for each question `"<question>"="<chosen label>"`; when the chosen option had a preview, ` selected preview:` + newline + the preview text; then a closing sentence inviting the model to proceed using the answers |
| `tool.AskUserQuestion.r.2` | noeta today: r.1, in our wording. |
| `tool.AskUserQuestion.r.3` | (bundle, not recorded) When an answer is not made of listed labels (the user typed their own "Other" text), the lead and closing change: the model is told the reply may ask for clarification, changes or a stop, and to follow what it actually says. noeta today: covered, in our wording. |
| `tool.AskUserQuestion.r.4` | (bundle, not recorded) A call with a question of fewer than 2 options is refused with a steer: nothing was shown; do not retry or invent a second option; state the single path as the approach and continue; re-ask only the questions with 2–4 distinct options. Repeated question texts, or repeated labels within a question, are refused. No length limit applies to any text (the 12-character header is guidance). noeta today: covered, in our wording; noeta also refuses an empty question or label and texts far past what a dialog shows (`deviations.md`). |

## Variants

Same hashes in every session.
