# docs/implementation-specs/ — implementation specs

A spec is the **working document for one piece of work**: its goal, its scope,
the decisions taken along the way, and the acceptance criteria that define
"done". It is written before or during implementation, and it is what a later
session reads to pick the work back up.

Everything in this directory describes **intent for work in flight**, not the
shipped system. For what the system does, read the code, the
[ADRs](../adr/index.md), and [CONTEXT.md](../../CONTEXT.md).

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

Distill, then delete.

1. Every decision worth keeping moves into an ADR under `docs/adr/` — the
   reasoning and the rejected alternatives, not the construction plan.
2. Every term the work pins down moves into `CONTEXT.md`.
3. What the system does is carried by the code and its tests.

Then `git rm` the spec. **There is no archive directory.** The durable content
sits in the ADRs and `CONTEXT.md`, where readers look for it; the construction
detail sits in the git history, next to the diff that produced it. A spec kept
past its work is a description of intent that some reader will mistake for
current design.

Work that gets called off ends the same way: if the direction is worth warning
the next person away from, write that into an ADR, then delete the file.

## Language

Specs are repository artifacts, so they are **written in English** like every
other doc here (`AGENTS.md`, "Language"). Technical terms keep their canonical
English form: code identifiers, API / library / tool / command names, file
paths, and fixed architecture terms.
