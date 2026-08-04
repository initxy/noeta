Retrieves the output of a background shell started with `Bash` `run_in_background: true`.

- Give the `bash_id` the launch returned. Each call returns ONLY the output produced since your previous check (like tailing a log), plus a status line (`running`, or `exited`/`killed` with the exit code).
- Optional `filter`: a regular expression applied per line — only matching lines of the new output are returned.
- A buffer-overflow note means the job printed faster than you polled and the oldest undelivered output was dropped.
- Safe and cheap to call repeatedly. To stop the job use `KillShell`.
