Reads a file's contents from the workspace, optionally sliced by line.

- `path` is workspace-relative. An absolute path is rejected unless it sits under a skill's `Base directory for this skill:` (shown when a skill activates) — use that base + the skill body's relative reference to read a skill's bundled files. Symlink escapes are always rejected.
- Reads the whole file by default. `offset` (1-based line) and `limit` (line count) take a slice — use them for large files.
- The full body is always offloaded as an artifact (`content_ref`); when inline output would exceed the byte budget you get a bounded excerpt plus the ref, never a silently truncated middle.
- A text target must be valid UTF-8; non-image binary files are rejected (use the right tool for those).
- Reads image files too (png, jpg, gif, webp): the image is presented to you visually, so you can read screenshots, diagrams, and photos directly.
- Don't guess a path — locate it first with `grep` (by content) or `glob` (by name).
