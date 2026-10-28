# noeta-hello

An example noeta plugin, packaged on its own: a `Greet` tool, state folded from the task log,
a reminder, and a `/greet` slash command. See `src/noeta_hello/__init__.py`.

The entry point in `pyproject.toml` is what makes it a plugin:

```toml
[project.entry-points."noeta.plugins"]
hello = "noeta_hello:Hello"
```

Once installed next to noeta, name it in an agent file:

```yaml
plugins:
  - hello: {greeting: Hi}
```

or add it to the main agent from `~/.noeta/config.toml`:

```toml
[plugins.hello]
greeting = "Hi"

[agents.main]
add_plugins = ["hello"]
```

`make example` at the repository root builds and installs it in a throwaway environment and
runs `tests/`.
