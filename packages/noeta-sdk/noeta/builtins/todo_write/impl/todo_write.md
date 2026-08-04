Use this tool to create and manage a structured task list for the current session.

## When to use

- Complex multi-step work (roughly three or more distinct steps), or the user handed you several tasks at once.
- Mark a todo `in_progress` BEFORE you start it and `completed` the moment it is fully done — keep exactly ONE item `in_progress` at a time.
- Update the list in real time as work lands; you may batch a TodoWrite with your next tool calls in the same turn.

## When NOT to use

- A single, straightforward step, or a purely conversational / informational request — the bookkeeping overhead is not worth it.

## Shape

A call replaces the entire list — always send the FULL list, never a delta; omitting an item deletes it. Each item is `{content, status, activeForm}`: `content` in imperative form ("Run tests"), `status` one of `pending` / `in_progress` / `completed`, `activeForm` in present-continuous form ("Running tests"), shown while the item is in progress.

Only mark `completed` when the item truly succeeded; if it is blocked, errored, or tests still fail, keep it `in_progress` and add a follow-up item.
