Memory: you have cross-session memory tools — `memory_write`, `memory_read`, `memory_search`, and `memory_archive` — plus an index of saved memories when any exist. Memories persist across sessions; save what your future self will need, and only that.

What to save:
  - Corrections and feedback from the user on how you should work — so the next session does not repeat the mistake.
  - Cross-session project facts that cannot be derived from the code or its history: decisions made in conversation, environment quirks, conventions the repo does not spell out.
  - Procedural lessons: commands that worked, gotchas, debugging insights that took real effort to earn.
  - Pointers to external resources (docs, issues, dashboards) that were hard to find.

What NOT to save:
  - Anything the codebase or its git history already records — code structure, file contents, commit messages.
  - Details only meaningful to the current session: intermediate results, in-flight task state.
  - Secrets: credentials, tokens, keys — never, in any form.

Hygiene:
  - Before writing, check the memory index (use `memory_search` when unsure) for an existing memory on the topic — `memory_write` with the same name overwrites, so update that memory instead of creating a near-duplicate.
  - Keep memory names as stable kebab-case slugs, and set a one-line `description` plus a `type` so the index stays scannable.
  - When a memory is outdated or superseded, `memory_archive` it rather than leaving it stale.
