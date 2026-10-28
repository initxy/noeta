# Read

Claude Code version: 2.1.312 (baseline session `m1`).

| source | sha256 (normalized) |
|---|---|
| `m1/0001 tools[Read].description` | `1177e0915dcf6e7ab82310e5209449af815b18b7a44626115c8f9935b20c21f8` |
| `m1/0001 tools[Read].input_schema` | `8523af52d9bea896c8c1a3bf8354bf3626a6598a0e2b76bcd1fe30c1398e7d20` |

## Purpose and placement

Reads one file (text, image, PDF or notebook) from the local filesystem. Always loaded; sixth in
the baseline list (after `ListAgents`). Identical in every recorded session and in sub-agents.

noeta equivalent: `noeta/plugins/fs/read.py`, description `noeta/plugins/fs/descriptions/read.md`.

## Parameters

JSON Schema 2020-12, `additionalProperties: false`, a description on each property.

| id | name | type | required | enum / default / limits | noeta today |
|---|---|---|---|---|---|
| `tool.Read.p.file_path` | `file_path` | string | yes | absolute path | contradicts: noeta also accepts workspace-relative paths and says so |
| `tool.Read.p.offset` | `offset` | integer | no | minimum 0, maximum 2^53-1; the 1-based line to start at; give it only when the file is too large for one read | contradicts: noeta minimum is 1, no maximum, no "only when too large" text |
| `tool.Read.p.limit` | `limit` | integer | no | exclusive minimum 0 (so at least 1), maximum 2^53-1; line count; give it only when the file is too large | covered (minimum 1); no property text |
| `tool.Read.p.pages` | `pages` | string | no | page range for PDFs (`"1-5"`, `"3"`, `"10-20"`), PDFs only, at most 20 pages per call | covered |
| `tool.Read.p.allow_large` | `allow_large` | boolean | no | true reads a file or line range beyond the normal size limits, up to what fits in context; reserved for when every part is needed or the user wants the entire file; otherwise read in pages or search | missing |

## Rules

| id | rule | noeta today |
|---|---|---|
| `tool.Read.1` | `file_path` must be absolute. | contradicts (relative accepted) |
| `tool.Read.2` | Without `limit`, at most 2000 lines are returned. | covered |
| `tool.Read.3` | When the needed part is known, read only that part; this matters most for large files. | covered |
| `tool.Read.4` | Output is in `cat -n` style with numbering starting at 1. | covered (but see `tool.Read.r.1` for the exact form) |
| `tool.Read.5` | Images (PNG, JPG and similar) are shown to the model visually. | covered |
| `tool.Read.6` | PDFs are read through `pages`; at most 20 pages per call; `pages` is required for PDFs longer than 10 pages. | covered |
| `tool.Read.7` | Jupyter notebooks (`.ipynb`) are returned as cells with their outputs. | covered |
| `tool.Read.8` | Reading a directory, a path that does not exist, or a zero-length file yields an error or a reminder rather than file content. | covered (directory and missing are errors; empty file is an inline parenthesised note, not a reminder) |
| `tool.Read.9` | Do not re-read a file just edited to check it: a failed `Edit`/`Write` errors, and the harness tracks file state. | covered (noeta's reason is the returned snippet, not tracked state) |
| `tool.Read.10` | The description names no byte or token cap; the over-limit escape is `allow_large`. | contradicts: noeta states a 256 KB / 25000-token failure with no override |
| `tool.Read.11` | The description says nothing about long-line truncation. | contradicts: noeta adds a 2000-character line cut (extra rule) |

## Result shape

| id | case | shape | noeta today |
|---|---|---|---|
| `tool.Read.r.1` | text file | one line per file line: the line number with **no padding**, a tab, the line text (`1\timport logging`); lines joined by `\n` | contradicts: noeta right-aligns the number to width 6 |
| `tool.Read.r.2` | file ending in a newline | a final numbered empty line is shown (`59\t` after line 58) | contradicts: noeta shows no line for the final newline |
| `tool.Read.r.3` | `offset`/`limit` given | numbering continues from the file's real line numbers (`offset: 2000` starts at `2000\t`); exactly `limit` lines; **no** footer or continuation note | contradicts: noeta appends "Showing lines a-b of n ... Use offset=..." whenever the read starts past line 1 or stops short |
| `tool.Read.r.4` | large window | a 1001-line, ~84000-character read came back whole, no truncation (`m3/0010`) | covered for size (under noeta's 25000-token cap? borderline: ~21000 tokens) |
| `tool.Read.r.5` | trailing context | other reminders (attribution, mid-turn user message) can ride in the same user message after the result | see `reminders.md` |

## Variants

None: `m1`, `m2`, `m3`, `m4`, `s1` and sub-agents all send the same text and schema.
