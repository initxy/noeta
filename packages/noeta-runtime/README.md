# noeta-runtime

The kernel under [noeta-sdk](https://pypi.org/project/noeta-sdk/). It records
every agent task as an append-only event log, schedules the work, and replays
a task from its log after a crash or a long wait — the part of
[Noeta](https://github.com/initxy/noeta) that makes agents durable. Pure
Python, standard library only, no HTTP client. Apache-2.0.

## You probably want noeta-sdk

```bash
pip install noeta-sdk      # pulls in noeta-runtime at a matching version
```

Build agents against `noeta.sdk`. The modules in this package are the
implementation beneath that surface, not an API: they can move between
releases without notice.

## Two rules the kernel is built on

- **`noeta.sdk` is the only public surface.** Hosts import `noeta.sdk`;
  nothing in `noeta-runtime` is imported directly.
- **The kernel carries no capabilities.** Tools, model adapters, memory, MCP,
  sandboxes, durable storage backends and the ReAct policy are built-in
  plugins that ship in `noeta-sdk` and load by reference through the plugin
  loader. Installed alone, this package runs an agent only from protocol
  objects you hand it — which is how its own tests exercise it.

## Learn more

- [Documentation](https://initxy.github.io/noeta/) — quickstart, guides, and
  how the engine works
- [Changelog](https://github.com/initxy/noeta/blob/main/CHANGELOG.md)
