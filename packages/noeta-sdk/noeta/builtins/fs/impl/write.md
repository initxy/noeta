Writes a file to the workspace, creating it or overwriting an existing one.

- Creating a new file needs no prior `Read`, and missing parent directories are created. Overwriting an existing file requires you to have `Read` it this session (and its content not to have changed since), so you never blindly clobber a file you haven't seen.
- `file_path` is workspace-relative. A path outside the workspace (absolute, `..`, or through a symlink) needs the owner's authorization for that directory; without it the call fails — don't route around that, say what you need and why.
- `content` is UTF-8 text and always the FULL file body — never write a partial file or use placeholders. The change is recorded as a unified diff artifact.
- For a small change to an existing file, prefer `Edit` — it replaces a surgical region instead of the whole body.
