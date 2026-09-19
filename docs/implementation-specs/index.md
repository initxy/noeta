# docs/implementation-specs/ — implementation specs

A spec is the **working document for one piece of work**: its goal, its scope,
the decisions taken along the way, and the acceptance criteria that define
"done". It is written before or during implementation, and it is what a later
session reads to pick the work back up.

The **top level** of this directory holds only **work in flight** — intent, not
the shipped system. Everything under `archive/` is a spec whose work already
landed, kept for the construction detail it records; its status line says which
release it shipped in. For what the system does today, read the code, the
[ADRs](../adr/index.md), and [CONTEXT.md](../../CONTEXT.md) — never an archived
spec.

## When to write one

`AGENTS.md` ("Workflow") asks for a spec when the work is complex or crosses
sessions. In practice, one of:

- the work spans several modules or several sittings, so the reasoning has to
  survive a context boundary;
- the scope is contested and worth settling before code gets written;
- the work is split across subagents, and each of them needs the same statement
  of goal and acceptance criteria to build against.

Small, self-contained changes need no spec. A document that only restates the
diff costs more than it returns.

A spec that earns its place states four things: the **goal** (what is true once
it is done), the **scope** — including explicit non-goals, the **key
decisions** and the alternatives they beat, and **acceptance criteria** concrete
enough to check against `make check` and a review.

## Spec vs ADR

- **Spec** — *how this particular change gets built*, and what "done" means for
  it. Bounded to one effort, and disposable.
- **[ADR](../adr/index.md)** — *why the system is shaped this way*, and which
  alternatives were rejected. Outlives every spec that touches it.

The test: if a sentence would make sense to someone who never saw the change, it
belongs in an ADR — or in `CONTEXT.md`, if it pins down what a term means. If it
only makes sense while the work is in flight, it belongs in the spec.

## When the work lands

Distill first, then archive. Distilling is the part that matters; the move is
bookkeeping.

1. Every decision worth keeping moves into an ADR under `docs/adr/` — the
   reasoning and the rejected alternatives, not the construction plan.
2. Every term the work pins down moves into `CONTEXT.md`.
3. What the system does is carried by the code and its tests.

Only then, `git mv` the file into `archive/` under a `YYYY-MM-DD-<name>.md`
name — the date it shipped, not the date it was written — and rewrite its
status line to `Status: SHIPPED <date> in <package> <version>`, naming where
the content was distilled to. Use `git mv` so the history follows the file.

Archiving rather than deleting keeps the construction detail — the slice plan,
the deviations, the rejected shapes that never reached an ADR — findable by
name instead of only by digging through a diff. The rule that makes it safe is
the split: the top level is current intent, `archive/` is history, and neither
is a description of the current design. That is what the ADRs and `CONTEXT.md`
are for.

Work that gets called off ends the same way: if the direction is worth warning
the next person away from, write that into an ADR, then archive the file with a
status line that says it was abandoned and why.

## Language

Specs are repository artifacts, so they are **written in English** like every
other doc here (`AGENTS.md`, "Language"). Technical terms keep their canonical
English form: code identifiers, API / library / tool / command names, file
paths, and fixed architecture terms.
