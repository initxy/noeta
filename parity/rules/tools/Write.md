# Write

Claude Code version: 2.1.312 (baseline session `m1`).

| source | sha256 (normalized) |
|---|---|
| `m1/0001 tools[Write].description` | `c5d31bd0010938b80d96805065d6e6602454d90b1c4fe2ec6420db47b2037fa6` |
| `m1/0001 tools[Write].input_schema` | `4d6f10d5833484ece535152d7bc1ffb1e22ad6a2b0ee60070ca2958e9aab7743` |

## Purpose and placement

Writes a whole file, creating or overwriting it. Always loaded; last of the always-loaded tools
in the baseline list (after `DeferredToolPlaceholder` in the first request; once any deferred tool
is loaded, `Write` moves before `DeferredToolPlaceholder`).

noeta equivalent: `noeta/plugins/fs/write.py`, description `noeta/plugins/fs/descriptions/write.md`.

## Parameters

JSON Schema 2020-12, `additionalProperties: false`, a description on each property.

| id | name | type | required | enum / default / limits | noeta today |
|---|---|---|---|---|---|
| `tool.Write.p.file_path` | `file_path` | string | yes | absolute path; the property text stresses absolute, not relative | covered (stated; relative paths still accepted) |
| `tool.Write.p.content` | `content` | string | yes | full file content | covered (noeta also caps the size) |

## Rules

| id | rule | noeta today |
|---|---|---|
| `tool.Write.1` | Purpose line: writes a file, overwriting any existing one. | covered |
| `tool.Write.2` | Use it to create a file or to replace a file already read in full. | covered |
| `tool.Write.3` | Overwriting an existing file that was not read first fails. | covered (also fails when changed since the read; skipped like `Edit`'s for a write allowed outright, never after a partial view) |
| `tool.Write.4` | For a partial change use `Edit`. | covered |
| `tool.Write.5` | The description says nothing about parent directories, outside-workspace paths, placeholders or diffs. | covered |
| `tool.Write.6` | Observed: the plan file is written with `Write` under the config dir's `plans/` folder, outside the project (`m1/0060`). | see `reminders.md` / plan mode |
| `tool.Write.7` | Runtime: a directory path is refused with a hint to name the file; an overwritten file keeps its UTF-8 byte-order mark; the file-state note is always there (`CLAUDE.md` included). | covered |

## Result shape

| id | case | shape | noeta today |
|---|---|---|---|
| `tool.Write.r.1` | new file | `File created successfully at:` + absolute path, then the parenthesised "file state is current, no need to read it back" note | covered |
| `tool.Write.r.2` | overwrite | same wording as `Edit`'s success: the file (absolute path) was updated successfully, plus the same note | covered |

## Variants

None across `m1`-`m4` and `s1`.
