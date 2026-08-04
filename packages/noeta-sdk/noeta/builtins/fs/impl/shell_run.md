Executes a shell command in the workspace and returns its output.

- Runs with `cwd` at the workspace root and a scrubbed (secret-free) environment. Output is stdout (plus a labeled stderr section when both streams have content), capped at 30000 characters with the middle elided; the full streams are recorded for audit.
- `timeout` is in milliseconds: default 120000, max 600000. A non-zero exit or a timeout comes back as an error carrying the same output.
- Provide a short `description` (5–10 words) of what the command does — it is shown to the user while the command runs.
- `run_in_background: true` launches the command detached (a server, a long build or test run): you get a shell ID back immediately and keep working. Read its incremental output with `BashOutput`, stop it with `KillShell`. A background job's lifetime follows the session, not this task.
- On a strict host the command must match an allowlist (git status/diff, pytest, npm/pnpm test, and read-only grep/rg/find/ls) and shell metacharacters are rejected; otherwise the full command runs through bash (pipes, redirection, chaining) and anything not on the allowlist needs a one-time approval.
- The process is not sandboxed — it runs real workspace code, so use only in a trusted workspace.
- To read a file, search content, or list files, prefer `Read` / `Grep` / `Glob` — they are cheaper and need no approval. Avoid `cat`/`grep`/`find` here for those jobs.
