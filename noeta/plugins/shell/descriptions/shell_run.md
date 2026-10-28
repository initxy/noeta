Runs a bash command and returns what it printed.

- A `cd` carries over to the next call only in the main conversation (never in a sub-agent), only when the command succeeds, and only when it ends inside the working directories; a command that ends outside them sends the next call back to the starting folder.
- Prefer absolute paths: a `cd` inside a compound command can make the call need approval.
- Variables and functions you define do not survive between calls.
- What a command prints is shown to you, and the user may not see it: repeat anything they need in your reply.
- `timeout` is in milliseconds: 120000 by default, 600000 at most for a command in the foreground.
- `run_in_background: true` detaches the command: it keeps running across turns and you are woken when it exits. No trailing `&` is needed. While it runs, do not poll its output file; you will be notified.
- A long `sleep` in the foreground is refused. To wait for something, use Monitor with an until-loop.

Git:
- Interactive flags (`-i`, as in `git rebase -i` or `git add -i`) do not work here.
- Use the `gh` CLI for GitHub work: pull requests, issues, the API.
- On GitHub an `@name` notifies that user. Write a bare `@name` only for a real account you mean to ping; put anything else (roles, placeholders, code, text taken from project files) in backticks or drop the `@`.
- Commit or push only when the user asks; on the default branch, create a branch first.
- When an attribution reminder is present, close every commit message and pull request description with its lines.
