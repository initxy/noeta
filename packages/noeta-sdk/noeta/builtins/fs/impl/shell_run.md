Executes a shell command in the workspace and returns its output.

- Runs with `cwd` at the workspace root and a scrubbed (secret-free) environment. Output is stdout (plus a labeled stderr section when both streams have content), capped at 30000 characters with the middle elided.
- `timeout` is in milliseconds: default 120000, max 600000. A non-zero exit or a timeout comes back as an error carrying the same output.
- Provide a short `description` (5–10 words) of what the command does — it is shown to the user while the command runs.
- `run_in_background: true` launches the command detached (a server, a long build or test run): you get a shell ID back immediately and keep working. Read its incremental output with `BashOutput`, stop it with `KillShell`. A background job's lifetime follows the session, not this task.
- The command runs through bash (pipes, redirection, chaining). The host decides which commands run freely, which need the user's approval, and which are refused — a refusal comes back as an error that says why.
- To read a file, search content, list files, or edit text, prefer `Read` / `Grep` / `Glob` / `Edit` — they are cheaper and need no approval. Avoid `cat`/`head`/`tail`/`grep`/`find`/`sed` here for those jobs.
