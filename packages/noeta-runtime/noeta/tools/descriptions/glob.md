Matches a glob pattern against file names and returns the matching paths.

- Standard glob semantics — `**` matches recursively (e.g. "**/*.py", "src/**/test_*"). Results are sorted and capped — narrow the pattern if truncated.
- Scope with `path` (the directory to search, default: the whole workspace).
- `path` is workspace-relative, or absolute to search outside the workspace (a neighbouring checkout). Results are workspace-relative POSIX paths, or absolute when the search was rooted outside it.
- `pattern` is always relative to `path`: no leading `/` and no `..`. Matches whose real path escapes the searched directory via symlink are dropped.
- To search by file content rather than name use `grep`; to read a known file use `read`.
