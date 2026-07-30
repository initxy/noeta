Maintain a structured task list for the current task by rewriting it whole.

## What it does

A single call replace-alls the entire checklist — you always send the FULL
list, never a delta. Each item is `{id, content, status}` where `status` is one
of `pending`, `in_progress`, or `completed`. Omitting an item deletes it.

## When to use

- The task is multi-step or non-trivial (roughly three or more distinct steps),
  or the user handed you several tasks at once.
- Mark a todo `in_progress` BEFORE you start it and `completed` the moment it is
  fully done — keep exactly ONE item `in_progress` at a time.
- Update the list in real time as work lands, so it always reflects reality.

## When NOT to use

- A single, straightforward step, or a purely conversational / informational
  request — the bookkeeping overhead is not worth it.
- Work that is one or two actions; just do it and report.

## Preconditions

- The `todo_write` capability must be enabled for this agent, otherwise the tool
  is not offered.
- Only mark `completed` when the item truly succeeded; if it is blocked, errored,
  or tests still fail, leave it `in_progress` and add a follow-up item.
