Retrieves the output of a background shell started with `Bash` `run_in_background: true` or with `Monitor`.

- Give the `bash_id` the launch returned. Each call returns ONLY the output produced since your previous check (like tailing a log), plus a status line (`running`, or `exited`/`killed` with the exit code).
- `block: true` waits for the command to finish before answering, up to `timeout` milliseconds (default 30000, max 600000); the status line says when it is still running after the wait. Use it instead of `sleep` whenever you need a background command's result before going on.
- Optional `filter`: a regular expression applied per line — only matching lines of the new output are returned.
- A buffer-overflow note means the job printed faster than you polled and the oldest undelivered output was dropped.
- Without `block`, safe and cheap to call repeatedly. To stop the job use `KillShell`.
