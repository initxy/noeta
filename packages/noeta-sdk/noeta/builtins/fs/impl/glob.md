Matches a glob pattern against file names and returns the matching paths.

- Standard glob semantics — `**` matches recursively (e.g. "**/*.py", "src/**/test_*"). Results are sorted by modification time, newest first, and capped with a notice — narrow the pattern if truncated.
- Scope with `path` (the directory to search, default: the whole workspace). `path` is workspace-relative, or absolute to search outside the workspace (a neighbouring checkout). Results are workspace-relative POSIX paths, or absolute when the search was rooted outside it.
- `pattern` is always relative to `path`: no leading `/` and no `..`. The walk is ripgrep's: gitignored and hidden files are not listed and symlinks are not followed; a `path` targeting a hidden directory directly is still searched.
- To search by file content rather than name use `Grep`; to read a known file use `Read`.
