# run_tests — a tool plugin

A `RunTests` tool that runs the project's test command where the task's commands run (this
machine, or a container exec env), records each result in the task log, and reminds the model
once when files changed since the last run. The reminder state is folded from the log, so a task
resumed on another worker knows whether it already nudged.

Install: `cp run_tests.py ~/.noeta/plugins/` — it joins the `main` agent by itself (`add_to`).
Set the command in `~/.noeta/config.toml`:

```toml
[plugins.run_tests]
command = "uv run pytest -q"
```

`make example` runs `tests/`.
