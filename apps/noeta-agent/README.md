# noeta-agent

The official Noeta **coding-agent app shell**: the HTTP/SSE backend
(`noeta.agent.backend`) over `noeta.sdk`, the bundled web app, slash command
contents, built-in skills, and the launch entry point. `python -m noeta.agent`
is the only entry — there is no `noeta` console script and no operator CLI.

Part of the [Noeta](https://github.com/initxy/noeta) workspace. Apache-2.0.

```bash
uv pip install -e apps/noeta-agent   # pulls in noeta-sdk + noeta-runtime
python -m noeta.agent                # boots the offline stub agent + bundled web
```
