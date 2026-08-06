Reads a file from the filesystem.

- `file_path` is workspace-relative, or absolute to read anywhere on this machine — a neighbouring checkout, a skill's `Base directory for this skill:` (shown when a skill activates), a system file. Reading is not fenced; only writing is.
- Reads up to 2000 lines from the beginning by default. `offset` (1-based line) and `limit` (line count) page through larger files — when you already know which part you need, read just that part; it matters for large files. Lines longer than 2000 characters are truncated with a marker.
- Output uses `cat -n` format: a right-aligned line number, a tab, then the line. Use the numbers to pick `offset` values and to cross-reference `Grep` results — never copy them into text you pass to `Edit` or `Write`.
- A file must be Read before `Edit` or `Write` may modify it.
- Reads images too (png, jpg, gif, webp): the image is presented to you visually. A text target must be valid UTF-8; PDFs and other binary files are rejected.
- Don't guess a path — locate it first with `Grep` (by content) or `Glob` (by name).
- Never re-read a file just to verify your own edit: a failed `Edit`/`Write` errors loudly, and a successful `Edit` already returns a snippet of the changed region.
