# noeta-runtime

The Noeta **engine**: the pure kernel (`protocols`, `core` = Engine + fold +
snapshot, and the kernel services — Worker / Dispatcher / ToolRuntime /
RuntimeLLMClient, storage, guards, observers, read models) **plus the agent
materials** that run on it (`policies`, `tools`, `providers`, `context`), the
execution machinery, the agent identity layer (`agent` = AgentSpec / registry)
and the official preset quartet. Everything needed to run an agent in-process.

Part of the [Noeta](https://github.com/initxy/noeta) workspace. Apache-2.0.

```bash
pip install -e packages/noeta-runtime
```
