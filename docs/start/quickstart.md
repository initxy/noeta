# Quickstart

Get a real agent working on your files in five minutes.

## 1. Install

```bash
uv pip install noeta-sdk        # or: pip install noeta-sdk
```

Python 3.11 or newer. Everything you import comes from `noeta.sdk`.

## 2. Set your API key

```bash
export ANTHROPIC_API_KEY=sk-ant-...
```

Using OpenAI or another gateway? See [step 5](#_5-use-another-model).

## 3. Run an agent

Save this as `agent.py` in any project directory and run `python agent.py`:

```python
from noeta.sdk import Options, query
from noeta.sdk.providers import AnthropicProvider

result = query(
    Options(system_prompt="You are a concise coding assistant."),
    goal="What files are in this directory, and what does each one do?",
    provider=AnthropicProvider(),
    model="claude-sonnet-5",
)
print(result.answer())
```

The agent lists the directory, reads the files it needs, and answers. The
wording varies from run to run; for a two-file project it looks like:

```
The directory contains two files:

- app.py — a one-line Python script that prints "hi".
- README.md — a README with just the heading "demo".
```

What you just used:

| Name | What it is |
|---|---|
| `Options` | The agent: system prompt, tools, permissions. With no `allowed_tools` it gets the built-in set (read, search, edit files, run shell commands, fetch web pages). |
| `query` | Runs one task to the end and returns the result. |
| `AnthropicProvider()` | The model connection. Reads `ANTHROPIC_API_KEY`. |
| `model` | Any model id your endpoint serves. |

The agent works in the current directory. Pass `workspace_dir="path"` to
`query` to point it elsewhere.

## 4. Look at what happened

`result` is also the full record of the run — every model call, tool call and
token count, in order:

```python
for event in result:
    print(event.seq, event.type)

for message in result.messages():   # the conversation, readable
    print(message)
```

```
0 TaskCreated
1 AgentBound
...
7 LLMRequestStarted
...
35 TaskCompleted
```

This log is what Noeta stores and replays. It is how a task survives a crash
and how you audit it later.

## 5. Use another model

Any OpenAI-compatible `/chat/completions` endpoint (reads `OPENAI_API_KEY`):

```python
from noeta.sdk.providers import OpenAICompatProvider

provider = OpenAICompatProvider(base_url="https://api.openai.com/v1")
result = query(options, goal="...", provider=provider, model="gpt-4o")
```

The agent itself does not change. Other options — the OpenAI Responses API,
custom gateways, models not in the catalog — are in
[Connect a model](../guides/models.md).

::: tip Risky calls ask first
By default, file edits (`Edit`, `Write`) and any shell command outside a
built-in safe list (`ls`, `git status`, …) pause the task for your approval
instead of running. `query` suits read-only jobs; the [tutorial](tutorial.md)
shows how to approve calls with `Client`. To skip approvals, set
`permission_mode="bypassPermissions"` — only where that is safe.
:::

## Next

- [Tutorial: build an agent](tutorial.md) — your own tool, approvals, a
  multi-turn conversation, and storage that survives a restart
- [Why Noeta](../why-noeta.md) — what sets it apart
- [Test offline & in CI](../guides/testing.md) — run agents with no API key
