Reads a file's contents, optionally sliced by line.

- `path` is workspace-relative, or absolute to read anywhere on this machine — a neighbouring checkout, a skill's `Base directory for this skill:` (shown when a skill activates), a system file. Reading is not fenced; only writing is.
- Reads the whole file by default. `offset` (1-based line) and `limit` (line count) take a slice — use them for large files.
- The full body is always offloaded as an artifact (`content_ref`); when inline output would exceed the byte budget you get a bounded excerpt plus the ref, never a silently truncated middle.
- A text target must be valid UTF-8; non-image binary files are rejected (use the right tool for those).
- Reads image files too (png, jpg, gif, webp): the image is presented to you visually, so you can read screenshots, diagrams, and photos directly.
- Don't guess a path — locate it first with `grep` (by content) or `glob` (by name).
