Launch a sub-agent to handle a focused, self-contained task and get its result back.

## What it does

Spawns one sub-agent per call: pick a `subagent_type` from the roster, give it the full task as `prompt`, and label it with a short `description` (3–5 words, shown to the user). The agent works in its own context and returns its final text as this call's result; that text is NOT shown to the user, so relay what matters.

**Parallelism**: emit SEVERAL Task calls in ONE assistant turn — they run CONCURRENTLY and their results come back together. Issuing one Task per turn is strictly sequential; when work is independent, always batch the calls into a single turn.

Once you have delegated a piece of work, do not also do it yourself — wait for the result and build on it.

## When to use

- The task fits a sub-agent type: a read-only scout for broad searches, a general-purpose worker for a self-contained coding task, an architect for a plan.
- Delegating independent work keeps your own context clean, or the answer means sweeping many files and you only need the conclusion, not the file dumps.

## When NOT to use

- A single-fact lookup where you already know the file or symbol — just look it up yourself.
- Work that needs your accumulated conversation context — a sub-agent starts fresh and sees ONLY its `prompt`, so make the prompt self-contained: the goal, the constraints, and what to return.
