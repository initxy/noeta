Searches file contents with ripgrep (rg regex syntax — linear-time; lookaround and backreferences are not supported).

- `pattern` matches line by line (or across lines with `multiline: true`). Scope with `path` (a file or sub-directory; workspace-relative, or absolute to search outside the workspace) and filter with `glob` (e.g. "**/*.py") or `type` (an rg file-type name, e.g. "py", "js", "rust").
- `output_mode`: "files_with_matches" (the default) lists the files that contain a match; "content" shows the matching lines; "count" shows per-file match counts.
- In content mode, line numbers are on by default (`-n: false` hides them; they align with `Read`'s), `context` (alias `-C`) or `-A` / `-B` add context lines, `-o: true` prints only the matched parts, and overlong lines are clipped with a marker.
- `-i: true` matches case-insensitively. `head_limit` caps the output like `head -N` — first N files/entries, or first N output lines in content mode (default 250; 0 = unlimited) — and `offset` skips the first N like `tail -n +N`.
- The walk carries rg's defaults: gitignored, hidden and binary files are skipped and symlinks are not followed. `-u: true` also searches gitignored and hidden files; a `path` targeting a hidden directory directly is always searched.
- Workflow: find the files with the default mode first, then `Read` the surroundings — it keeps search noise out of your context. To match by filename use `Glob`.
