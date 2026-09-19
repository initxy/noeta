Run a short Python orchestration script that fans work out to sub-agents and returns a result.

## What it does

The script runs in a sandbox that adds these names to the ordinary builtins:

- `parallel(items, agent="general-purpose")`: spawn a BATCH of sub-agents at
  once, wait for them all, and return their answers as a list in spawn order.
  Each item is a goal string, or a `{"goal": ..., "agent": ...}` dict to pick a
  specific sub-agent per item.
- `agent(goal, agent="general-purpose")`: spawn ONE sub-agent, wait for it, and
  return its final answer (a string). Sequential `agent()` calls run one after
  another, so chain them ONLY when a later call needs an earlier result; for
  independent work use `parallel()` instead.
- `args`: the dict supplied via this tool's `args` parameter.

Finish with `return <value>` — that value becomes the workflow's answer.

## When to use

- You need to ORCHESTRATE sub-agents programmatically: loop over a list, branch
  the next call on a prior result, or chain steps where each one feeds the next.

## When NOT to use

- For a single delegation — use `Task` instead.
- For plain parallelism with no loop / branch / dependency — emit several
  `Task` calls in one response; they run concurrently without a workflow.
- For work you can do yourself with the file/search/shell tools.

## Preconditions

- The script must be deterministic: it may be replayed from the top after a
  crash, so every run must make the same calls — no imports, no clock or
  randomness, no I/O of its own (`print` included); the sub-agents do the I/O.
  A script that breaks this is rejected before any sub-agent starts.

## Example

A dependency chain — the fan-out depends on the scout's output, which is what
needs a workflow:

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
