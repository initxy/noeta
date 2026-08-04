Kills a background shell started with `Bash` `run_in_background: true`.

- Give the `shell_id` the launch returned. Sends SIGTERM, then SIGKILL after a short grace. The call returns immediately; the job's exit is reported through the usual background-completion notice.
