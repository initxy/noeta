# Quickstart

Five minutes from an empty terminal to a finished agent turn — no API key, no
network, no server. You install one package, run one turn against a scripted
offline model, read the event ledger it produced, and then swap in a real
provider.

Everything you import comes from `noeta.sdk`, the SDK's single public surface.

## 1. Install

```bash
uv pip install noeta-sdk
```

`noeta-runtime` — the pure kernel — comes along as a transitive dependency you
never import directly. Python 3.11 or newer is required.

Check the install:

```bash
python -c "import noeta.sdk; print(noeta.sdk.Options)"
```

```
<class 'noeta.client.options.Options'>
```

## 2. Run one turn, offline

`FakeLLMProvider` is a scripted, deterministic LLM double. You hand it the
responses you want the model to "return" and it replays them in order — so the
first run needs no credentials and touches no network.

`Options` is the agent recipe. `query` builds a throwaway client, drives one
turn to a terminal state, and hands back everything that happened.

<!-- runnable: smoke -->
```python
from noeta.sdk import LLMResponse, Options, TextBlock, Usage, query
from noeta.sdk.testing import FakeLLMProvider

provider = FakeLLMProvider(
    responses=[
        LLMResponse(
            stop_reason="end_turn",
            content=[TextBlock(text="Hello from Noeta.")],
            usage=Usage(uncached=1, output=1),
        )
    ]
)

result = query(
    Options(
        system_prompt="You are concise.",
        allowed_tools=("Read",),
        permission_mode="bypassPermissions",
    ),
    goal="Say hello.",
    provider=provider,
    model="stub-model",
)

print(result.answer())
```

```
Hello from Noeta.
```

That is a complete agent turn: the goal was recorded, the model was asked, and
the task reached a `TaskCompleted` terminal.

## 3. Read the ledger

Noeta records every turn as an append-only stream of event envelopes, and task
state is always recomputed by folding that stream. `query` returns a
`QueryResult`, which **is** the `list[EventEnvelope]` plus two folded views of
it.

<!-- runnable: smoke -->
```python
from noeta.sdk import LLMResponse, Options, TextBlock, Usage, query
from noeta.sdk.testing import FakeLLMProvider

result = query(
    Options(
        system_prompt="You are concise.",
        allowed_tools=("Read",),
        permission_mode="bypassPermissions",
    ),
    goal="Say hello.",
    provider=FakeLLMProvider(
        responses=[
            LLMResponse(
                stop_reason="end_turn",
                content=[TextBlock(text="Hello from Noeta.")],
                usage=Usage(uncached=1, output=1),
            )
        ]
    ),
    model="stub-model",
)

# 1. The raw ledger — `result` IS a list of event envelopes.
for env in result:
    print(f"{env.seq:>3}  {env.type:<22}  actor={env.actor}")

# 2. The folded, human-readable projection.
print()
for item in result.messages():
    print(item)

# 3. Just the terminal answer.
print()
print(result.answer())
```

```
  0  TaskCreated             actor=engine
  1  AgentBound              actor=engine
  2  ModelBound              actor=engine
  3  ContextContentRecorded  actor=plugin:environment
  4  MessagesAppended        actor=engine
  5  TaskStarted             actor=engine
  6  ContextPlanComposed     actor=engine
  7  LLMRequestStarted       actor=llm
  8  LLMResponseRecorded     actor=llm
  9  LLMRequestFinished      actor=llm
 10  MessagesAppended        actor=engine
 11  TaskSnapshot            actor=engine
 12  TaskCompleted           actor=engine

UserMessage(text='Say hello.')
AssistantMessage(text='Hello from Noeta.')
Result(answer='Hello from Noeta.', status='completed')

Hello from Noeta.
```

Three ways to read the same run, from raw to cooked:

| Call | Returns | Use it for |
| --- | --- | --- |
| iterate `result` | `EventEnvelope` objects, in `seq` order | audit, debugging, assertions on what actually ran |
| `result.messages()` | the folded conversation view | showing a user what happened |
| `result.answer()` | the terminal answer only | the common case |

`answer()` is strict: it raises `QueryFailedError` if the task failed or never
reached a terminal, so a failure can never be mistaken for a successful answer.

## 4. Swap in a real provider

The provider is **wiring**, not identity — `compile_options` never reads it, and
it is excluded from equality. The same `Options` compiles to the same agent
whichever vendor serves it, so the two lines below are the whole change.

Anthropic (falls back to `ANTHROPIC_API_KEY` when `api_key` is omitted):

```python
from noeta.sdk.providers import AnthropicProvider

provider = AnthropicProvider(api_key="sk-ant-…")
result = query(options, goal="Say hello.", provider=provider,
               model="claude-sonnet-4-5-20250929")
```

Any OpenAI-compatible `/chat/completions` endpoint (falls back to
`OPENAI_API_KEY`):

```python
from noeta.sdk.providers import OpenAICompatProvider

provider = OpenAICompatProvider(base_url="https://api.openai.com/v1",
                                api_key="sk-…")
result = query(options, goal="Say hello.", provider=provider, model="gpt-4o")
```

The adapters live in `noeta.sdk.providers`, a lazy submodule, so importing
`noeta.sdk` does not pull in `httpx` until you actually build a network
provider. `model` must be an id your endpoint really serves.

## 5. Where to go next

- **Build a real agent** — [Your first agent](first-agent.md) adds a custom
  `@tool`, an approval gate, and a multi-turn `Client`.
- **Point at your own gateway** — [Configure a provider](../how-to/configure-provider.md).
- **Understand the ledger** — [Concepts](../concepts/index.md), starting with
  [Event sourcing](../concepts/event-sourcing.md).
- **Look up a signature** — [SDK reference](../reference/sdk.md).
