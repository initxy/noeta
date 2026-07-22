Searches file contents across the workspace with a regular expression (Python `re` syntax).

- Returns each match as file path + line number + the matching line. Locate files-with-matches first, then `read` the surrounding lines to keep search noise low.
- Scope with `path` (a file or sub-directory) and filter with `glob` (e.g. "**/*.py").
- `pattern` is a Python `re` regular expression. Binary files are skipped automatically.
- `path` is workspace-relative, or absolute to search outside the workspace (a neighbouring checkout). Symlinks are not followed, so one walk stays in one tree.
- To match by filename rather than content use `glob`; to read a known file use `read`.
