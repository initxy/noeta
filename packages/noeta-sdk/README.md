# noeta-sdk

The thin in-process **client surface** (`noeta.sdk` facade — `query` / `Client`
/ `Options` / `tool` / extension interfaces) over the
[noeta-runtime](https://github.com/initxy/noeta) kernel, plus **the built-in
plugin catalogue** (`noeta.builtins` — every official capability
implementation: the fs/web tool packs, provider adapters, guards, reminders,
memory, browser, app, MCP, sandbox backends) and the official presets. Like
claude-agent-sdk / LangChain: `import noeta.sdk`, run an agent in-process; no
engine internals, no HTTP server.

Part of the [Noeta](https://github.com/initxy/noeta) workspace. Apache-2.0.

```bash
pip install -e packages/noeta-sdk
```
