Searches file contents with a regular expression (Python `re` syntax).

- `pattern` matches line by line (or across lines with `multiline: true`). Scope with `path` (a file or sub-directory; workspace-relative, or absolute to search outside the workspace) and filter with `glob` (e.g. "**/*.py").
- `output_mode`: "files_with_matches" (the default) lists the files that contain a match; "content" shows the matching lines; "count" shows per-file match counts.
- In content mode, `-n: true` adds line numbers (they align with `Read`'s), `-A`/`-B`/`-C` add context lines, and overlong lines are clipped with a marker.
- `-i: true` matches case-insensitively. `head_limit` caps how many results return.
- Hidden directories and dependency/cache trees (node_modules, __pycache__, …) are skipped; binary and unreadable files are skipped silently. Symlinks are not followed, so one walk stays in one tree.
- Workflow: find the files with the default mode first, then `Read` the surroundings — it keeps search noise out of your context. To match by filename use `Glob`.
