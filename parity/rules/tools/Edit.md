# Edit

Claude Code version: 2.1.312 (baseline session `m1`).

| source | sha256 (normalized) |
|---|---|
| `m1/0001 tools[Edit].description` | `2eaaa8e08e0b58bc3700a39e70a054e7bfa1d3bfd048ef502df41dd1e7c0009f` |
| `m1/0001 tools[Edit].input_schema` | `a034471310194167437bf97444a001d7e5129768ae7da14714f1c370c4962800` |

## Purpose and placement

Replaces an exact string in an existing file. Always loaded; fourth in the baseline list (after
`Bash`). Not in the sub-agent tool set of `Explore` (see `subagent.md`).

noeta equivalent: `noeta/plugins/fs/edit.py`, description `noeta/plugins/fs/descriptions/edit.md`.

## Parameters

JSON Schema 2020-12, `additionalProperties: false`, a description on each property.

| id | name | type | required | enum / default / limits | noeta today |
|---|---|---|---|---|---|
| `tool.Edit.p.file_path` | `file_path` | string | yes | absolute path of the file to change | covered (stated; relative paths still accepted) |
| `tool.Edit.p.old_string` | `old_string` | string | yes | text to replace | covered (an empty string creates the file: `tool.Edit.9`) |
| `tool.Edit.p.new_string` | `new_string` | string | yes | replacement; must differ from `old_string` | covered |
| `tool.Edit.p.replace_all` | `replace_all` | boolean | no | default `false` (schema `default: false`); replace every occurrence | covered |

## Rules

| id | rule | noeta today |
|---|---|---|
| `tool.Edit.1` | Purpose line: exact string replacement in a file. | covered |
| `tool.Edit.2` | The file must have been read in this conversation first, or the call fails. | covered (noeta additionally fails when the file changed since the read) |
| `tool.Edit.3` | `old_string` must match exactly, indentation included, and must be unique; otherwise the edit fails. | covered |
| `tool.Edit.4` | Strip `Read`'s line prefix (line number and tab) before matching. | covered |
| `tool.Edit.5` | `replace_all: true` replaces every occurrence. | covered |
| `tool.Edit.6` | Observed: an `Edit` succeeded on files the model had only seen through a Bash `cat` in the same conversation (`m1/0042`, `m1/0066`). The bundle's mechanism: the read-first check is skipped when the write is allowed outright by the permissions (mode or rule), for all but older Claude models. | covered (skipped when the permission layer allowed the call outright, `ToolContext.allowed_outright`; kept for older Claude models and without a permission layer; `deviations.md`) |
| `tool.Edit.7` | The description says nothing about outside-workspace paths, diffs or snippets. | covered |

## Runtime behaviour (2.1.312 bundle)

| id | rule | noeta today |
|---|---|---|
| `tool.Edit.8` | A file changed on disk since the read: when the write is allowed outright and `old_string` still picks one place (or `replace_all`), the edit applies and the result says the file holds unseen changes; otherwise re-read. | covered |
| `tool.Edit.9` | An empty `old_string` creates the file (parents too) or fills an empty one; refused over content. No read needed. | covered |
| `tool.Edit.10` | Not found: the error repeats `old_string`; a missing file gets `Read`'s answer (working directory, a guess); an `.ipynb` is sent to `NotebookEdit`; a file over 1 GB is refused. | covered |
| `tool.Edit.11` | Straight quotes match curly ones and `new_string` takes the file's quote style; `\uXXXX` escapes match their characters both ways. | covered |
| `tool.Edit.12` | Deleting (`new_string` empty) takes the following line break along. CRLF files keep CRLF. | covered |
| `tool.Edit.13` | (bundle) A not-text file is refused with an error saying the tools never convert encodings; a UTF-16LE file is written back as UTF-16LE with its byte-order mark, a UTF-8 byte-order mark is kept. | covered |

## Result shape

| id | case | shape | noeta today |
|---|---|---|---|
| `tool.Edit.r.1` | success | one line: the file (absolute path) was updated successfully, followed by a parenthesised note that the file state is current in context and need not be read back. No snippet, no diff. | covered |
| `tool.Edit.r.2` | success on the project `CLAUDE.md` | the same line **without** the "file state is current" note (`m1/0066`). The bundle's rule: the note follows only when the model had the whole file in view, unchanged, before the edit (the `m1` file had changed since it was loaded; the r2 recording's `m1/0071` CLAUDE.md edit after a whole read has the note), and stays off for later edits. | covered |
| `tool.Edit.r.3` | two edits of one file in one turn | each call gets its own identical success line | covered |
| `tool.Edit.r.4` | `replace_all` | a different line: the file was updated and every occurrence replaced | covered |

## Variants

None across `m1`-`m4` and `s1`.
