Executes a shell command in the workspace and returns its output.

- Runs with `cwd` at the workspace root, a scrubbed (secret-free) environment, and capped output — the full stdout/stderr are offloaded as artifacts and you get the tail inline plus the return code.
- `timeout` is in milliseconds: default 120000, max 600000.
- `run_in_background: true` launches the command detached (a server, a long build or test run): the call returns a `job_id` and an output ref immediately and you keep working. Read its output with `shell_poll`, stop it with `shell_kill`. A background job's lifetime follows the session, not this task.
- On a strict host the command must match an allowlist (git status/diff, pytest, npm/pnpm test, and read-only grep/rg/find/ls) and shell metacharacters are rejected; otherwise the full command runs through bash (pipes, redirection, chaining) and anything not on the allowlist needs a one-time approval.
- The process is not sandboxed — it runs real workspace code, so use only in a trusted workspace.
- To only read a file, search content, or list files, prefer `read` / `grep` / `glob` — they are cheaper and need no approval.
