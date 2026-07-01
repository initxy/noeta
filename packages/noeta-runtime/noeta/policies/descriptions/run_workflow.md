Run a short Python orchestration script that fans work out to sub-agents and returns a result.

## What it does

Submits a model-authored orchestration script that runs in a deterministic
sandbox exposing exactly these names:

- `parallel(items, agent="general-purpose")`: spawn a BATCH of sub-agents at
  once, wait for them all, and return their answers as a list in spawn order.
  Each item is a goal string, or a `{"goal": ..., "agent": ...}` dict to pick a
  specific sub-agent per item. Use this for the fan-out step INSIDE a workflow —
  when you also need a loop / branch / dependency chain around it. For plain
  one-shot parallelism you do NOT need a workflow: just emit several
  `spawn_subagent` calls in one turn and they run concurrently.
- `agent(goal, agent="general-purpose")`: spawn ONE sub-agent, wait for it, and
  return its final answer (a string). Sequential `agent()` calls run one after
  another, so chain them ONLY when a later call needs an earlier result; for
  independent work use `parallel()` instead.
- `log(message)`: emit a progress note (returns nothing).
- `args`: the dict supplied via this tool's `args` parameter.

Finish with `return <value>` — that value becomes the workflow's answer. The
script is not a normal tool: it is interpreted as its own sub-task, so it can
suspend and resume across many sub-agent spawns and survive a crash.

## When to use

- You need to ORCHESTRATE sub-agents programmatically: loop over a list, branch
  the next call on a prior result, or chain steps where each one feeds the next.
- The work is multi-step across agents — a dependency chain (`agent()` feeding
  `agent()`), or fan-out batches you then loop over or combine.

For PLAIN parallelism — several independent sub-agents, no loop / branch /
dependency — do NOT reach for a workflow: emit multiple `spawn_subagent` calls
in one turn and they run concurrently.

## When NOT to use

- For a single one-off delegation — just use `spawn_subagent` instead; reaching
  for a whole script to wrap one spawn is overkill.
- For plain parallelism with no loop / branch / dependency — emit several
  `spawn_subagent` calls in one turn instead; they fan out concurrently without
  a workflow.
- For work you can do yourself with the file/search/shell tools; the sub-agents
  you spawn do the actual I/O, so a workflow only pays off when the work is
  multi-step or branches across agents.

## Preconditions

- The script MUST be deterministic: no time/random/datetime, no imports, no file
  or network access (the sub-agents you spawn do the actual I/O). Non-deterministic
  scripts are rejected before any sub-agent runs.
- Delegation must be enabled for this agent (the workflow spawns real sub-agents);
  if the host has not opted into workflows this tool is not offered at all.

## Example

A dependency chain — scout first, then fan the result out and combine. THIS is
what needs a workflow (the fan-out depends on the scout's output); plain
parallelism would just be several `spawn_subagent` calls in one turn:

    modules = agent(
        'List the modules missing a docstring, one bare name per line.',
        agent='explore',
    )
    parts = [m.strip() for m in modules.splitlines() if m.strip()]
    docs = parallel(
        ['Write a one-line docstring for module: ' + m for m in parts],
        agent='general-purpose',
    )
    return '\n'.join(docs)
