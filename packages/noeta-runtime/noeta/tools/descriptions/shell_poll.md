Checks the status and latest output of a background shell job.

- Give the `job_id` returned by `shell_run` with `run_in_background: true`. Returns the status (`running` or `exited`), an `exit_code` once finished, and a ref to a fresh snapshot of the output so far — dereference the ref to read the bytes.
- `truncated: true` means the output overflowed the cap and the snapshot is the most-recent tail (oldest output dropped). Safe and cheap to call repeatedly.
- To stop the job use `shell_kill`; to start a new command use `shell_run`.
