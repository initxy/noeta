# house_style — a prompt and reminder plugin

Claude Code's output styles for noeta. The style file is read once, when a task first starts,
and recorded in the task log, so a task resumed later or on another worker sends the same
system prompt bytes even after the file changed (new tasks get the new style). The style sits
in the cached system block; the reply language rides as a recorded reminder on each of the
person's turns.

Install: `cp house_style.py ~/.noeta/plugins/` — it joins every agent by itself (`add_to`).

```toml
[plugins.house_style]
file = "~/.noeta/output-styles/house.md"
language = "French"
```

The texts are named prompts (`house_style/style`, `reminder/house_style`), overridable like any
other. `make example` runs `tests/`.
