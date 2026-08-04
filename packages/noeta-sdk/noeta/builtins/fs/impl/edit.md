Performs an exact string replacement in an existing file.

- You must have `Read` the file this session before editing it, and its content must not have changed since — otherwise the call fails and you Read it again first.
- `old_string` must match the file contents exactly — including whitespace and indentation — and must match exactly once. Zero matches or several matches fail and write nothing; include more surrounding context to make it unique. Never include `Read`'s line-number prefix in `old_string` or `new_string`.
- `old_string` and `new_string` must differ. `new_string` may be empty to delete the matched region.
- Set `replace_all` to true to replace every occurrence instead — use it for renames of a recurring string.
- `file_path` is workspace-relative. A path outside the workspace needs the owner's authorization — the call pauses for their ruling on the directory.
- On success you get a `cat -n` snippet of the edited region to verify the result. The change is also recorded as a unified diff artifact.
- Creating a new file or replacing a whole file body is `Write`'s job, not this one's.
