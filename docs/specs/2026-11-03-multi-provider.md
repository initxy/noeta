---
status: active
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

- No kernel change: `Runtime.provider_for(model)` already routes per request by model id.
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
  provider first, then tables in file order), else the default provider. A switch logs only the
  model id (`CONFIG_SIGNAL`), so a resumed task routes the same on any machine with the same
  config. Why not `provider/model` in the log: plugins read the task model as a bare id
  (catalog lookups, `startswith("claude")`, family caps), and the kernel's `provider_for` is
  keyed by model id alone; a prefixed id would drift in a dozen places or need a kernel change.
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

## Acceptance criteria

- An existing single-provider config / env / flags behaves exactly as before.
- With two providers configured, a task switched with `/model` (or `set_model`, ACP) sends its
  next request to the other provider; a resumed task in a fresh app routes the same.
- `/model` with no argument lists the models of every provider.
- An uncatalogued model whose provider lists a context length runs at that window.
- All four gates green.

## Risks

- Same model id on two providers: the first by the routing order wins; no way to pick the other.
- A listing fetch blocks its caller up to its timeout once a day per endpoint (off the loop in
  async callers; synchronous at CLI start for an uncatalogued start model).

## Progress log

- 2026-11-03: baseline `make check` green (3515 passed); design settled per Decisions.
- 2026-11-03: router, listings, config tables, front ends, tests, docs in; `make check` green
  (3541 passed, 89.81%), `make parity` 0 mismatches.
- 2026-11-03: live smoke against the configured gateway: both its anthropic and openai_compat
  listings publish windows (10/10 rows); one call routed to each adapter answered.
  Consequence: the gateway's uncatalogued live model now runs at its listed 1,000,000 window
  (was the 128k default), output cap still 16,384 (the listing names none).
- 2026-11-03: `make cluster` 14 passed, `make e2e` 7 passed (ACP live run now reads the gateway listing).
