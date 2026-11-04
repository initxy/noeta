---
status: done
created: 2026-11-03
branch: multi-provider
---

# Several providers, one task

## Goal

The `noeta` command (and `worker` / `serve`, which build their app the same way) can name
several model providers at once, route each model to the provider that serves it, switch models
across providers mid-task (`/model`, stream-json `set_model`, ACP), list the models every
provider offers, and run each model at its real context window — including a gateway model the
catalog does not know, when its provider's `/models` listing publishes one.

## Non-goals

- No kernel capability: the kernel only keeps a task's recorded route and hands the host the
  task's state when it asks for a provider (see Decisions).
- No `provider/model` selector in the logged model id (see Decisions).
- No pricing from listings; an uncatalogued model still bills as unknown.
- A third-party provider family gets no listing (its models file / table rows give windows).

## Context

`noeta/cli/config.py` `Settings` holds one provider (family + base_url + api_key + headers);
`from_settings` hands `LocalApp` one `Provider`, so `/model` only switches among models of that
endpoint. The kernel calls `rt.provider_for(request.model)` for every model call and token count
(`noeta/core/runner/model.py`, `task.py`), and `LocalApp` already accepts a callable.

## Decisions

- Config: `[providers.<name>]` tables in `config.toml` with `family`, `base_url`, `api_key`
  (a literal or the usual `{env:NAME}` / `{keyring:…}` reference, resolved when the provider is
  first used), `headers`, `models` (ids, or rows `{id, context_window, max_output_tokens,
  supports_vision}` registered like models-file rows) and `list_models` (default true).
- The top-level `provider` / `base_url` / `api_key` / `extra_headers` (and their env vars and
  flags) stay the default provider, unchanged. `provider = "<table name>"` makes that table the
  default instead (a table name wins over a family name).
- Routing scheme (one): the logged model id stays the bare id the provider and every plugin
  knows. A model routes to the provider that names it (a table's `models`, or a models-file
  row's `provider`), else the first provider whose `/models` listing has it (the default
  provider first, then tables in file order), else the default provider. Why not
  `provider/model` in the log: plugins read the task model as a bare id (catalog lookups,
  `startswith("claude")`, family caps); a prefixed id would drift in a dozen places.
- (review, superseding "a switch logs only the model id") Resolution is made once, when a model
  is chosen, and recorded: config key `model_route` = `{model, provider, context_window?,
  max_output_tokens?, supports_vision?}` (`MODEL_ROUTE`), written by `LocalClient` on create and
  on any `CONFIG_SIGNAL` carrying `model` (so the TUI, ACP, stream-json, `-p --resume` and the
  HTTP server all record it). A worker sends that model to the recorded provider and fails the
  call (`Fatal`) when it is not configured there; other models resolve on the worker. Limits in
  the record are registered when the task is loaded (`env_for`, the first host hook that sees
  the folded state, before the first request is sized). A sub-agent on its parent's model
  inherits the record (`subtasks.child_config`). One provider: nothing is recorded unless a
  listing gave the limits, and `task_provider` is not set.
- (re-review, superseding the parts above it contradicts) Routes are recorded and followed
  only with more than one provider; with one nothing is attached and the log is byte for byte
  main's (a `None` route is never written). A child task records its own model's route when it
  is written (`Runtime.prepare_child`, at `TaskRun.append`, so the Agent tool, workflows and any
  `TaskView.spawn` are covered); the `subtasks` inheritance is gone. A recorded route's limits
  are registered from `task_provider`, which the runner asks each time it builds a task's view
  (its token counter), after the config fold and before the request is sized; the `env_for`
  wrapper is gone. Clients cannot set a route: the HTTP guard leaves `model_route` out of the
  config keys a request may set or change, stream-json `set_model` refuses it, ACP takes only
  `model` / `mode`. Exceptions that resolve per worker (README lists them): plugin side-call
  models (`compaction_model`, title / classifier models, a skill's `model`) and the agent's own
  model after an editor picks "default".
- (review) Kernel change, minimal: `MODEL_ROUTE` joins `CHANGEABLE_CONFIG` (a switch must record
  the route atomically with the model), and `Runtime.task_provider(state, model)` /
  `Runtime.provider_of` (3 call sites) let the host pick with the task's state. Needed because
  `provider_for(model)` sees only the model id, and plugin side calls (`TaskView.complete`) and
  token counts bypass every extension hook, so no plugin could carry the route to them.
  (re-review) Plus `Runtime.prepare_child` (one call in `TaskRun.append`): children are staged
  synchronously inside tools and plugins, and their model (an agent's own) is known only to the
  runtime; the hook is the one place every spawn passes, async so a listing read stays off the
  loop.
- (review) Listings: 404 / 405 = no listing (kept empty a day); any other failure keeps the older
  copy untouched (its age too) and retries after 60 s, doubling to the max age. Start reads a
  listing only for an uncatalogued start model; routing listings are read when a task is given
  a model.
- (review) With several endpoints, `Thinking.family` is tagged `<family>@<endpoint>` on the way
  in and restored only for that endpoint, so signed reasoning goes back only to its signer.
  The Anthropic wire rewrites tool-call ids outside `^[a-zA-Z0-9_-]+$` (stable: sanitized + hash).
- (review) One model named for two providers (tables, models file) is a config error naming both.
- Routing lives in `noeta/backends/providers/routing.py` (`ProviderRouter`): `provider_for`
  returns the endpoint's adapter itself when the route is known without network, else a proxy
  that resolves the route off the event loop on its first call. One endpoint: no listing is read
  for routing.
- Window: catalog, then models file / table rows, then the routed provider's `/models` listing
  (`context_length`, `context_window`, `max_input_tokens`, `max_model_len`,
  `top_provider.context_length`), registered as a catalog row; else the 128k default + notice.
  Listings are read only from the configured endpoint with its own key, cached per endpoint in
  `~/.noeta/cache/models/` for a day, a failure falling back to a stale copy or to nothing.
- `from_settings` builds the default provider eagerly (a missing key still stops the start);
  another table's adapter is built on first use, a failure failing only that call (`Fatal`).
- Tests never read a listing over the network: `tests/conftest.py` stubs `fetch_listing`.
- Front ends reach this through two optional `LocalApp` members set by `from_settings`:
  `list_models()` (model, provider) and `prepare_model(model)` (route + window, returns a notice
  or `None`). Without them (a remote app, an embedder) the catalog-only behavior stays.

## Plan

- [x] Spec
- [x] `noeta/backends/providers/listing.py`: fetch + parse a provider's `/models`
- [x] `noeta/backends/providers/routing.py`: `Endpoint`, `ProviderRouter` (route, cache, windows)
- [x] `noeta/cli/config.py`: `[providers.*]` tables, models-file `provider`, router into `LocalApp`
- [x] `LocalApp.list_models` / `prepare_model`; TUI `/model`, stream-json, ACP use them
- [x] Tests: config parsing, routing, switching across providers, window lookup
- [x] Docs: README config, `docs/extending.md` provider row
- [x] Gates: `make check`, `make parity`, `make cluster`, `make e2e`
- [x] Review fixes: recorded routes, listing failure handling, limits on load, replay across
      providers, lazy start, model conflicts, tests through `from_settings`

## Acceptance criteria

- An existing single-provider config / env / flags behaves exactly as before.
- With two providers configured, a task switched with `/model` (or `set_model`, ACP) sends its
  next request to the other provider; a resumed task in a fresh app routes the same.
- `/model` with no argument lists the models of every provider.
- An uncatalogued model whose provider lists a context length runs at that window.
- All four gates green.

## Risks

- Same model id on two providers: a config error when both name it; when only listings have
  it, the first by the routing order wins.
- A listing read blocks its caller up to its timeout (off the loop in async callers;
  synchronous at start only for an uncatalogued start model, so an offline endpoint costs that
  start up to 5 s).
- Models a task uses without a front-end choice (an agent file's own model, a side call) still
  resolve per worker; deterministic only when configuration names them.

## Progress log

- 2026-11-03: baseline `make check` green (3515 passed); design settled per Decisions.
- 2026-11-03: router, listings, config tables, front ends, tests, docs in; `make check` green
  (3541 passed, 89.81%), `make parity` 0 mismatches.
- 2026-11-03: live smoke against the configured gateway: both its anthropic and openai_compat
  listings publish windows (10/10 rows); one call routed to each adapter answered.
  Consequence: the gateway's uncatalogued live model now runs at its listed 1,000,000 window
  (was the 128k default), output cap still 16,384 (the listing names none).
- 2026-11-03: `make cluster` 14 passed, `make e2e` 7 passed (ACP live run now reads the gateway listing).
- 2026-11-03: independent review: listing-based routing was per worker, failures cached as
  empty, limits registered after the first request was sized, signed reasoning crossed
  same-family endpoints, start read every listing, duplicate model names overwrote. Fixed per
  the (review) Decisions; the route record needed a minimal kernel change (reason there).
- 2026-11-03: review fixes green: `make check` 3556 passed (89.83%), parity 0 mismatches, cluster 14 passed, e2e 7 passed.
- 2026-11-03: re-review: sub-agents on another model and plugin side-call models had no route,
  mid-run switches registered limits only at run start, one provider logged `model_route`
  (null on switches), clients could forge a route. Fixed per the (re-review) Decision.
- 2026-11-03: re-review fixes green: `make check` 3559 passed (89.82%), parity 0 mismatches, cluster 14 passed, e2e 7 passed.
- 2026-11-04: final items: a failing child route falls back to none (warned); the view-before-sizing order is commented and tested (`tests/core/test_task_provider.py`). Gates green: check 3561 passed (89.83%), parity 0, cluster 14, e2e 7.
