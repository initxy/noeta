Writes a file to the workspace, creating it or overwriting one you've already read.

- `path` is workspace-relative and its parent directory must already exist. A path outside the workspace (absolute, `..`, or through a symlink) needs the owner's authorization: the call pauses, they approve the directory once, and it stays open afterwards. Don't route around a pause — say what you need and why.
- Creating a new file always works. Overwriting an existing file requires you to have `read` it earlier in this session, so you never blindly clobber a file you haven't seen.
- `content` is UTF-8 text, max 64 KB. The change is offloaded as a unified diff with before/after hashes.
- For a small change to an existing file, prefer `edit` — it sends a surgical diff instead of replacing the whole body.
