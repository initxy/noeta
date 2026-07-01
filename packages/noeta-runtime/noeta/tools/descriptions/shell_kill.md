Stops a background shell job you started.

- Give the `job_id` from a `shell_run` call with `run_in_background: true`. Sends SIGTERM, then SIGKILL after a short grace. The call returns immediately; the job's final output snapshot stays readable via its ref.
- Use it to stop a server, build, or test run you launched and no longer need (e.g. started on the wrong port and want to restart).
- To only check status or read output, use `shell_poll` instead.
- Privileged: an operator policy may require approval or deny it.
