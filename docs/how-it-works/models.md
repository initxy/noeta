# Providers and model routing

This page explains how noeta talks to model APIs: the one provider interface, the three wire
families it ships, the model catalog that sizes every call, how a model is routed to one of
several configured providers so that every worker sends it to the same place, and what happens
when a call fails. Setting providers up is in [Models and providers](../guides/models.md); the
keys are in [configuration keys](../reference/config.md).

## The provider interface

A provider is one async call from a request to a response (`noeta/core/provider.py`):

```python
class Provider(Protocol):
    family: str          # the wire it speaks: anthropic, openai_compat, openai_responses
    async def complete(self, request: ModelRequest,
                       on_delta: OnDelta | None = None) -> ModelResponse: ...
    # optional: count_tokens(model, text), complete_whole(request), aclose()
```

The request and the response are noeta's own types (`noeta/core/types.py`): a `ModelRequest`
holds the system parts, messages, tools, `max_tokens`, effort, thinking, cache marks, headers
and the few vendor opt-ins an adapter may read (`betas`, `vendor_fields`); a `ModelResponse`
holds the reply message, the stop reason, the model and `Usage` (`uncached`, `cache_read`,
`cache_write`, `output`, `reasoning`). Every vendor detail lives in its adapter, so the kernel,
the log and every plugin stay provider-neutral, and a task can switch providers mid-conversation.

Adapters never retry. Each turns a failure into one neutral error (`noeta/core/errors.py`):
`Transient` (network, 5xx, overloaded), `RateLimited` (429, with `Retry-After`), `StreamBroken`,
`TimedOut`, `MalformedToolArguments`, `ContextOverflow`, or `Fatal` (auth, a bad request, a
refusal). One helper decides what to do with them ([below](#retries-and-timeouts)).

Providers are found by family through the `noeta.providers` entry points, so a new model API is
a package that registers one. A provider cannot come from a plugin file: the plugin pin does not
cover providers, and a provider on a single worker would break "any worker resumes any task".

## The three families

| Family | Endpoint | What the adapter does |
|---|---|---|
| `anthropic` (`noeta/backends/providers/anthropic.py`) | `<base_url>/v1/messages` (the base URL is the host) | The body Claude Code sends an API-key user: its beta set, adaptive thinking, effort, `context_management`, `metadata.user_id`, three cache marks, host notes as mid-conversation `system` turns and `<system-reminder>` blocks. Streams every call; a stream that breaks after it began goes on as the same body sent in one piece (`complete_whole`). Counts tokens through `/v1/messages/count_tokens`. Takes `prompt_cache_ttl`. |
| `openai_compat` (`openai_compat.py`) | `<base_url>/chat/completions` (the base URL ends in `/v1`) | Chat Completions for OpenAI, vLLM, OpenRouter, LiteLLM, DeepSeek and most gateways. Host text goes as a `system` message that says it is not the user. Tool names longer than the wire takes are shortened and restored. Streams every call and parses the folded result once. |
| `openai_responses` (`openai_responses.py`) | the whole `…/v1/responses` URL, posted as given | The Responses API's `input` / `output` items. Every request carries `store: false`, so nothing is left on the gateway and a resumed task never depends on it; encrypted reasoning items are sent back as the API needs them to continue. |

## The model catalog

`noeta/core/models.py` holds one `ModelSpec` per model: context window, output ceiling, the
`max_tokens` sent by default, prices (input, output, cache read, cache write), whether it reasons,
whether it takes images, its display name, knowledge cutoff and default effort. The aliases
`opus`, `sonnet`, `haiku` and `fable` resolve to Claude models.

The catalog is kernel data because the kernel uses it on every call: it caps each call's output
(`Runtime.output_ceiling`, always sent, since a gateway once defaulted to 1,000 tokens when it was
left out), the compaction plugin derives its thresholds from it, adapters decide thinking and
images from it, and `price()` turns recorded usage into cost. Plugins and providers read the same
table, so a model has one row everywhere.

**A model the catalog does not know** is a warning, never a silent default: it gets a
conservative 128,000-token window and 16,384-token output cap (compaction still runs), its images
are let through for the provider to judge, and its price is unknown: its cost counts as $0.00 with a
warning, so a cost budget under-reports. Rows are
added at run time with `register_models`, from a models file (`NOETA_MODELS_FILE`), from rows in a
`[providers.<name>]` table's `models`, or from a provider's own `/models` listing when it
publishes limits.

## Routing with several providers {#routing-with-several-providers}

More providers go in `config.toml` tables beside the default one:

```toml
[providers.claude]
family = "anthropic"
api_key = "{env:ANTHROPIC_API_KEY}"
models = ["claude-opus-5-5"]
prompt_cache_ttl = "1h"

[providers.deepseek]
family = "openai_compat"
base_url = "https://api.deepseek.com/v1"
api_key = "{env:DEEPSEEK_API_KEY}"
models = ["deepseek-flash", "deepseek-v4-pro"]
list_models = true            # read its /models for more models and their windows
```

`ProviderRouter` (`noeta/backends/providers/routing.py`) sends each call to the provider that
serves its model. A model id resolves to:

1. the provider whose table (or a models-file row's `"provider"`) names it; one model named for
   two providers is a config error;
2. else the first provider whose `/models` listing has it, the default provider first;
3. else the default provider.

A model's limits come from the catalog, then a models file or table rows, then its provider's
listing when that publishes one, else the default limits with a warning. Listings are read only
when a decision needs one, from the configured endpoint with its own key
(`noeta/backends/providers/listing.py`), and kept a day in `~/.noeta/cache/models`. A 404 or 405
means the endpoint has none; any other failure keeps the older copy and is tried again after a
pause that starts at 60 seconds and doubles.

<Diagram name="model-routing" caption="A model's provider is decided once and recorded; every worker follows the record" />

**The decision is recorded.** Only the configuration part of that resolution is the same on every
machine: a listing may differ between machines, or fail on one. So when a task is started with a
model or switched to one (`/model`, the headless protocol's `set_model`, an editor's pick), and
when a sub-agent is spawned on its parent's model or its own, the server resolves the model once
and records the result in the task's config as `model_route`: the provider's name and any limits
a listing gave, beside the bare model id (`ProviderRouter.route_record`, called from
`LocalApp`'s client and from `Runtime.prepare_child`). Every worker then sends that model to the
recorded provider at those limits (`task_provider`), whatever its own listings say. A worker where
that provider is not configured fails the call (`Fatal`) rather than send it elsewhere.

- Only the server records a route: a client (HTTP, stream-json, an editor) cannot set
  `model_route` (`noeta/host/_http_guard.py`, `noeta/host/_stream_json_control.py`).
- With a single provider nothing is recorded and the log is as it always was; each process looks
  a model's listed limits up itself.
- **Side models resolve on the worker that runs them**: a plugin's model call beside the
  conversation (`compaction_model`, a title or permission-classifier model, `webfetch_model`, a
  skill's `model`), and the agent's own model after an editor picks "default". Those are the same
  everywhere only when a table or models file names the model.
- `/model` with no argument lists every provider's models.

## Reasoning and call ids across providers

A conversation can move between providers, so two things a provider wrote must not reach another
that would refuse them:

- **Signed reasoning.** A `Thinking` block's signature or encrypted data must round-trip byte for
  byte, and only to the family that issued it (`Thinking.family`); the Anthropic adapter drops
  another family's thinking. With several endpoints the router also tags each block
  `<family>@<endpoint>` on the way in and restores the family only for that endpoint on the way
  out, so two endpoints of the same family never get each other's signed reasoning.
- **Tool-call ids.** The runner gives every tool call an id no earlier call of the task has
  (`unique_call_ids`, `noeta/core/runner/model.py`): answers, approvals and children are keyed by
  call id, and a gateway that sends no id or reuses one must not let one call inherit another's
  answer. The Anthropic adapter rewrites an id it does not accept (one another provider wrote)
  into its alphabet plus a hash of the original, the same every time.

## Retries and timeouts {#retries-and-timeouts}

<Diagram name="model-retry" caption="complete_with_retries: the one place that decides whether to try again" />

`complete_with_retries` (`noeta/core/retry.py`) wraps every model call a task makes, the turn's
own and every plugin's `view.complete` / `ctx.complete`:

- **Transient errors and rate limits** are retried with exponential backoff: 1, 2, 4 … up to 30
  seconds, half fixed and half random (so two sub-agents that hit the same 429 do not retry in
  lockstep), or exactly the provider's `Retry-After`. Nine attempts in all, about two minutes of
  waiting.
- **A stream that broke after it began** (`StreamBroken`) goes on as the same request answered in
  one piece, when the provider offers it (Anthropic's `complete_whole`, output capped at 64,000,
  300 seconds), as Claude Code falls back from a failed stream. A one-piece call that times out
  goes back to streaming and never switches again.
- **Tool-call arguments that do not parse** (`MalformedToolArguments`, almost always a stream cut
  mid-call) are asked for once more.
- **An overflow that states its numbers** is sent once more with a smaller output cap
  ([context](./context.md#staying-inside-the-window)); any other `ContextOverflow` goes to the
  plugins' `on_context_overflow`, and `Fatal` fails the turn with `llm_error`.

Each retry is recorded as a core `retry` record (attempt, delay, error), so front ends show
"retrying" and drop the failed attempt's streamed text, and the log says what happened. The runner
adds one guard of its own: a model call started three times without a recorded reply (the worker
crashed mid-call each time) fails the turn instead of looping.

**Timeouts.** Every call streams, so a timeout measures silence, not the length of the answer:

| Limit | Value |
|---|---|
| Wait for the response headers | 600 s (Claude Code's request timeout; a gateway that holds the stream until the answer is complete sends nothing before then) |
| Silence on the stream | Anthropic 60 s, OpenAI shapes 300 s (`timeout_seconds`) |
| A call answered in one piece | 300 s for the whole call |
| A token count | 10 s; an estimate stands in when it passes |

Cancelling a turn cancels the call at once, during the request or during a backoff sleep.

**Replies that are not usable.** A reply cut at the output cap is asked to go on, at most three
times in a row; one cut while still reasoning, with no text yet, is not recorded (only its usage
is). An empty `end_turn` reply
goes to the plugins' `on_empty_reply` (the `empty_reply` plugin asks the model to continue).

## Calls beside the conversation

A plugin or tool calls the model with `view.complete` / `ctx.complete`: through the task's
providers, retried like the turn's call, but not a step of the conversation. What such a call
costs is recorded as a core `spent` record (with a purpose, such as `compaction`), so a task's
cost and its USD budget count it. A call whose result decides later requests comes back through
the inbox ([ADR-031](https://github.com/initxy/noeta/blob/main/docs/adr/031-background-plugin-work-returns-through-the-inbox.md)).

The spending limits are on the [context page](./context.md#budgets).
