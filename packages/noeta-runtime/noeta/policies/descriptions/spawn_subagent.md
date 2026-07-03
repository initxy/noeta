Delegate focused goals to named sub-agents and get their results back.

## What it does

Spawns the sub-agents listed in `spawns` — each entry picks an `agent` from
the roster and gives it a `goal`, an independent sub-task. ONE entry delegates
and waits for that single result. SEVERAL entries fan out and run
CONCURRENTLY; you suspend once and all results come back together, in entry
order. A sub-agent's final text is returned to you as the result; that text is
NOT shown to the user, so relay what matters.

## When to use

- The goal fits a sub-agent type: a read-only scout for broad searches, a
  general-purpose worker for a self-contained coding task, an architect for a
  plan.
- Delegating independent work keeps your own context clean, or the answer means
  sweeping many files and you only need the conclusion, not the file dumps.
- You have SEVERAL independent goals: put them ALL in the `spawns` array of a
  single call — that is what makes them run in parallel. **If the user asks to
  run sub-agents "in parallel", you MUST batch the goals into one call's
  `spawns` array.** A call with a single entry suspends you until that one
  child returns, so one-entry-per-turn is strictly sequential — never parallel.
  Spawning one, then "the next after it finishes", does NOT fan out; batch
  them.

## When NOT to use

- A single-fact lookup where you already know the file or symbol — just look it
  up yourself.
- Orchestrating sub-agents programmatically — loop over a list, branch the next
  spawn on a prior result, or chain a dependency where each step feeds the next —
  use `run_workflow` instead. (Plain parallelism is not this: just batch the
  entries in one call, see above.)
- Work already delegated — do not re-spawn for the same thing.

## Preconditions

- Delegation must be enabled for this agent, otherwise the tool is not offered.
- Pick each entry's `agent` only from the roster names shown on the parameter;
  authorization is enforced by the permission guard, not by this schema.
- `background=true` is only valid with exactly ONE `spawns` entry (a fan-out
  batch always runs in the foreground).
