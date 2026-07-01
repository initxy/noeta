Delegate a focused goal to a named sub-agent and get its result back.

## What it does

Spawns ONE sub-agent — picked by `agent`, constrained to the roster of allowed
names — to work on `goal`, an independent sub-task. The sub-agent runs to
completion and its final text is returned to you as the result; that text is NOT
shown to the user, so relay what matters.

## When to use

- The goal fits a sub-agent type: a read-only scout for broad searches, a
  general-purpose worker for a self-contained coding task, an architect for a
  plan.
- Delegating independent work keeps your own context clean, or the answer means
  sweeping many files and you only need the conclusion, not the file dumps.
- You want SEVERAL independent sub-agents at once: emit multiple `spawn_subagent`
  calls in the SAME turn and they fan out and run concurrently, with results
  returned in call order. This is the direct way to parallelize — plain
  parallelism does NOT need a workflow. **If the user asks to run sub-agents "in
  parallel", you MUST emit all the `spawn_subagent` calls in a single
  turn.** A single `spawn_subagent` suspends you until that one child returns, so
  spawning one per turn is strictly sequential — never parallel. Spawning one at a
  time, then "the next after it finishes", does NOT fan out; batch them.

## When NOT to use

- A single-fact lookup where you already know the file or symbol — just look it up
  yourself.
- Orchestrating sub-agents programmatically — loop over a list, branch the next
  spawn on a prior result, or chain a dependency where each step feeds the next —
  use `run_workflow` instead. (Plain parallelism is not this: just spawn several
  at once, see above.)
- Work already delegated — do not re-spawn for the same thing.

## Preconditions

- Delegation must be enabled for this agent, otherwise the tool is not offered.
- Pick `agent` only from the roster names shown on the parameter; authorization is
  enforced by the permission guard, not by this schema.
