Activate a named skill so its instructions load into the current task.

## What it does

A single call activates ONE skill, chosen by the `skill` parameter from the
skill listing; its instructions are in your context from your next step. A
listing line may be shortened to save context —
the skill behind it is complete, and activating it loads everything.

Make it the only tool call in its response. Batched with other tool calls,
those calls run but the skill is not loaded.

## When to use

- The task matches an available skill — activate it BEFORE producing other output
  about the task, so its guidance is in force while you work.
- The user references a skill by name or types `/<name>`.

## When NOT to use

- The skill you want is not in the listing — never guess or invent a name; pick
  only from the listed ones.
- A skill is already active — do not re-activate it.
- No listed skill covers the task — just proceed without one.
