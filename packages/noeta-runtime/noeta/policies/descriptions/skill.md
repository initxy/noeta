Activate a named skill so its instructions load into the current task.

## What it does

A single call activates ONE skill, chosen by the `skill` parameter — constrained
to the roster of skills indexed for this workspace. Activation loads that skill's
instructions and capabilities via a state patch, the same channel a pre-loop
activation uses. There are no other arguments: just the skill name.

## When to use

- The task matches an available skill — activate it BEFORE producing other output
  about the task, so its guidance is in force while you work.
- The user references a skill by name or types `/<name>`.

## When NOT to use

- The skill you want is not in the roster — never guess or invent a name; pick
  only from the listed ones.
- A skill is already active — do not re-activate it.
- No listed skill covers the task — just proceed without one.

## Preconditions

- The `skill_invocation` capability must be enabled AND the workspace must have at
  least one indexed skill, otherwise the tool is not offered.
- The `skill` value must be one of the names in the roster shown on the parameter.
