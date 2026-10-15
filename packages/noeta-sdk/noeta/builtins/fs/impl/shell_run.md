Executes a shell command in the workspace and returns its output.

The working directory persists between commands, but shell state does not. The first command starts at the workspace root, and a command that leaves the workspace sends the next one back there.

- Runs with a scrubbed (secret-free) environment. Output is stdout (plus a labeled stderr section when both streams have content), Output past 30000 characters is saved to a file and you receive a short preview plus the path; `Read` it with `offset`/`limit` for the rest.
- `timeout` is in milliseconds: default 120000, max 600000. A non-zero exit or a timeout comes back as an error carrying the same output.
- Provide a short `description` (5–10 words) of what the command does — it is shown to the user while the command runs.
- You can use the `run_in_background` parameter to run the command in the background. Only use this if you don't need the result immediately. You get a shell ID back at once; `BashOutput` reads its output (with `block: true` it waits for the command to finish) and `KillShell` stops it. A background job's lifetime follows the session, not this task.
- Avoid unnecessary `sleep` commands:
  - Do not sleep between commands that can run immediately — just run them.
  - Do not retry failing commands in a sleep loop — diagnose the root cause.
  - Long leading `sleep` commands are blocked. To poll until a condition is met, use an until-loop (e.g. `until <check>; do sleep 2; done`). Do not chain shorter sleeps to work around the block.
  - If waiting for a background task you started with `run_in_background`, call `BashOutput` with `block: true` — do not poll or sleep.
- Try to maintain your current working directory throughout the session by using absolute paths and avoiding usage of `cd`. You may use `cd` if the User explicitly requests it.
- The command runs through bash (pipes, redirection, chaining). The host decides which commands run freely, which need the user's approval, and which are refused — a refusal comes back as an error that says why.
- To read a file, search content, list files, or edit text, prefer `Read` / `Grep` / `Glob` / `Edit` — they are cheaper and need no approval. Avoid `cat`/`head`/`tail`/`grep`/`find`/`sed` here for those jobs.
