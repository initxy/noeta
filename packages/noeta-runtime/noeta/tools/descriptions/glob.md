Matches a workspace-relative glob pattern and returns the matching file paths.

- Standard glob semantics — `**` matches recursively (e.g. "**/*.py", "src/**/test_*"). Results are workspace-relative POSIX paths, sorted, and capped — narrow the pattern if truncated.
- `pattern` must be workspace-relative: no leading `/` and no `..`. Matches whose real path escapes the workspace via symlink are dropped.
- To search by file content rather than name use `grep`; to read a known file use `read`.
