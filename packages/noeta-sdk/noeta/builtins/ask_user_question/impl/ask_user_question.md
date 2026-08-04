Use this tool only when you are blocked on a decision that is genuinely the user's to make: one you cannot resolve from the request, the code, or sensible defaults.

- Presents 1–4 questions at once. Each question has the complete `question` text (ending with a question mark), a very short `header` chip (max 12 chars, e.g. "Auth method"), 2–4 `options` (each `{label, description}` — concise label, trade-off in the description), and `multiSelect` (true when several answers may be picked together).
- Do not add an "Other" option — the user can always type a custom answer; it is provided automatically.
- If you recommend an option, put it first and append "(Recommended)" to its label.
- The user's selections are returned so you can proceed.

When NOT to use: a reasonable default exists (make it, state the assumption, keep working); "should I proceed?" / "is this right?" check-ins; facts you can verify yourself in the codebase.
