# docs/implementation-specs/ — implementation specs

A spec is the **working document for one piece of work**: the goal, the scope,
the decisions taken along the way, and the acceptance criteria. It is written
before or during implementation (`AGENTS.md`: "write complex or cross-session
implementation specs into `docs/implementation-specs/`"), and it is what a
later session reads to continue the work.

A spec is not an ADR. The split:

- **Spec** — *how this particular change gets built*, and what "done" means for
  it. Bounded to one effort. Goes stale the moment that effort lands.
- **[ADR](../adr/index.md)** — *why the system is shaped this way*, and which
  alternatives were rejected. Outlives every spec that touched it.

When a spec produces a decision worth keeping, that decision belongs in an ADR;
the spec keeps the construction detail.

## Status

Every spec carries a `> **Status:**` blockquote directly under its title. There
are three values:

| Status | Meaning | Lives in |
| --- | --- | --- |
| `Active` | being implemented now, or waiting to be | this directory |
| `Shipped` | the work landed | `archive/` |
| `Abandoned` | the work was dropped or superseded | `archive/` |

A `Shipped` / `Abandoned` line says **what landed and where** — the commit,
release, or ADR — so a reader can jump to the real thing instead of trusting a
document that describes intent:

```markdown
> **Status: Shipped** — landed in 0.3.2; the durable decisions live in
> [anchored-content-placement.md](../adr/anchored-content-placement.md).
```

## Archiving

When work lands, set the status and `git mv` the file into `archive/`. Do not
delete it and do not rewrite it to match what shipped: an archived spec is a
record of *how the work was reasoned about at the time*, which is exactly what
makes it useful when someone revisits the area. If reality diverged from the
plan, say so in the status line rather than editing the body.

The archive is not a graveyard to be skipped — it is the first place to look
before rebuilding something. What it must never be is a place a reader mistakes
for current intent, which is what the status line prevents.

## Language

Specs are repository artifacts, so they are **written in English** like every
other doc here (`AGENTS.md`, "Language"). Technical terms keep their canonical
English form: code identifiers, API / library / tool / command names, file
paths, and fixed architecture terms.
