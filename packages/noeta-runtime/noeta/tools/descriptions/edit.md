Replaces an exact, unique `old` string in an existing file with `new`.

- `old` must match exactly once — zero matches or more than one fails and writes nothing. Include enough surrounding context to make it unique, or target a region that already is.
- Set `replace_all` to true to replace every occurrence of `old` instead — use this for renames or repeated edits where the same string recurs and you want them all changed.
- `path` is workspace-relative and must be an existing UTF-8 text file; absolute paths and symlink escapes are rejected.
- `new` may be empty to delete the matched region. The change is offloaded as a unified diff with before/after hashes.
- Creating a new file or replacing a whole file body is `write`'s job, not this one's.
