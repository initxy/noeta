# noeta-runtime

The Noeta **engine**: a pure kernel — `protocols` (the only typed boundary),
`core` (Engine + fold + snapshot), the kernel services (Worker / Dispatcher /
ToolRuntime / RuntimeLLMClient, storage, observers, read models), the material
mechanisms (`context` = the locked composer + registries, `policies` = the
control band, `tools` = authoring machinery), the injection-only `execution`
builder, and the agent identity layer (`agent` = AgentSpec / registry). It
carries **no capability implementation and no HTTP client**: installed alone
it runs an agent with hand-injected protocol objects; the official
capabilities (tool packs, provider adapters, guards, memory, browser, app,
MCP, sandbox backends, skills, the ReAct policy) ship as built-in plugins
inside the [noeta-sdk](https://github.com/initxy/noeta) wheel and arrive
through the plugin loader.

Part of the [Noeta](https://github.com/initxy/noeta) workspace. Apache-2.0.

```bash
pip install -e packages/noeta-runtime
```
