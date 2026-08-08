# The model catalog is operator-extensible: a registration overlay beside the shipped table

## Context

The model catalog is a shipped, in-memory transcription of public vendor rate
cards: `CATALOG` (`model id → ModelSpec`: window, output cap, four optional
price tiers, reasoning/vision bits) and `ALIASES` (`"opus"`-style shorthand).
Cost accounting, the compaction water-mark derivation, the adapters' vision
guards and the vendor-family classification all read it. Deployments also run
models the public table cannot know — internal gateway routing names,
self-hosted models, private fine-tunes. Before this decision their operators
had two bad options: edit SDK source (unshippable, lost on upgrade, and it
leaks internal names if it ever escapes) or run uncatalogued (unknown pricing,
conservative compaction fallback, no vision decision).

## Decision

**Operator rows live in a module-level overlay, never in the shipped dicts.**
`register_models(models, aliases=None)` validates and commits into
`_EXTENSIONS` / `_EXTENSION_ALIASES`; `find_spec` — the single membership
judgment — consults the shipped table first, then the overlay, and every
consumer that used to reach into `CATALOG` directly goes through it. `CATALOG`
stays exactly the shipped table; `catalog_models()` is the merged enumeration
a host lists in a model picker.

**Two registration forms, one mechanism.** Declarative:
`HostConfig(extra_models={...})`, applied when the `Client` is constructed —
before anything derives from the catalog on any Client-driven path.
Imperative: `noeta.sdk.providers.register_models(...)` at process start, for
hosts that consult catalog-derived surfaces before building a Client. The
client plane reaches the catalog only through the `noeta.client.parts`
dynamic accessors, preserving the no-static-import-of-builtins contract.

**Collisions refuse; identical re-registration is a no-op.** A name already
present anywhere — shipped row, shipped alias, extension row, extension
alias — raises `ValueError` naming the id; so does re-registering a
*different* spec for a registered name, an unknown `provider_family`, an
empty id, or a non-`ModelSpec` value. Registering the identical frozen spec
again is silent, so several Clients may share one `HostConfig`. Registration
is all-or-nothing under a module lock: a refused call leaves the overlay
untouched, and racing registrations cannot interleave.

**Alias targets are resolved at registration and stored resolved**, so an
alias may point at shipped shorthand and `resolve_alias` stays single-hop.

**A row may declare its wire family explicitly.**
`ModelSpec.provider_family` (`"anthropic"` / `"openai"`, validated) is
consulted ahead of real-id prefix inference: a gateway routing name carries no
recognisable prefix, but the operator knows what speaks behind it. Shipped
rows keep inference, so the public table stays a pure transcription of vendor
pages. Nothing built-in branches on the family today; the field is a
declaration for hosts and future consumers.

**The determinism contract.** The catalog feeds
`derive_compaction_config`, which feeds composed prompt bytes, so a host must
register the same extensions on every run — the same requirement as wiring
the same plugins and skill directories. There is deliberately no unregister:
removal mid-process would change composed bytes under a live session.

**Absent pricing stays a state, not a zero.** An extension row served by a
gateway with no rate card registers `None` prices and inherits the existing
warn-once / charge-`0.0` behavior; a literal `0.0` still means genuinely
free.

## Alternatives considered

- **Threading a catalog object through every consumer** — rejected: lookups
  are module-level functions called across the client plane, the builtins and
  the sdk doorway; the churn is wide and purely mechanical, and the catalog is
  process-wiring, like loaded plugins — not per-session state.
- **A `plugin_config` entry instead of a `HostConfig` field** — rejected: that
  channel configures one session pack; the catalog is consumed pervasively
  and by no pack. The skills-tier precedent went the other way for exactly
  the mirrored reason (a single consumer).
- **Allowing extensions to override shipped rows** (negotiated prices,
  corrections) — deliberately not decided here; overriding has different
  failure modes than adding, and silent shadowing of published rates is the
  primary hazard this design refuses.
- **Per-session catalogs** — rejected: would fracture resume determinism.
- **Dynamic refresh from a vendor/gateway endpoint** — rejected: the catalog
  must stay a deterministic function of process wiring.
- **Directory-name-style mutation of `CATALOG` by the host** (the old
  documented workaround) — rejected and un-documented: it bypasses every
  collision rule, and an upgrade or a second import path silently loses it.

## Consequences

- Internal routing names live in host configuration, never in the published
  package.
- `CATALOG` alone under-reports on a host with extensions; enumeration goes
  through `catalog_models()`. Point lookups are already merged everywhere.
- Registration at Client construction means catalog-derived answers obtained
  *before* the first Client exists (`model_capabilities` at config-validation
  time) miss the extensions unless the host uses the imperative form at
  process start.
- `provider_family` gains a second source (declaration beside inference);
  the validation set must grow with any future family.
