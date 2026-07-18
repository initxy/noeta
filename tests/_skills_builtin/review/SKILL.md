---
name: review
description: Review the current git diff for correctness and adherence to repo standards
argument-hint: [base ref or scope, e.g. "since main" or a file path]
---

# Review changes

Act as an expert code reviewer for the current working changes. Be concise but
thorough, and focus on what actually matters.

Scope / base ref from the user (default to uncommitted changes if empty): $ARGUMENTS

## Steps

1. Establish the diff. Run `git_status` to see what changed, then `git_diff` to read
   the actual hunks. If the user named a base ref (e.g. `main`), pass it to `git_diff`
   so you compare against that point instead of just unstaged changes.
2. For any hunk you cannot judge in isolation, `read` the surrounding code and
   `grep` for callers, related tests, and existing conventions so your review reflects
   real repo context rather than guesses.
3. Report along two axes:
   - **Correctness** — logic errors, edge cases, error handling, concurrency, data
     loss, security issues, and whether tests cover the change.
   - **Standards** — does it match this repo's existing conventions, naming, structure,
     and style? Cite the convention you're comparing against.
4. For each finding give: file + location, what's wrong, why it matters, and a concrete
   suggested fix. Separate must-fix issues from optional nits. If something is fine,
   say so briefly rather than padding.

Do not modify code in this review — only report. Keep the output skimmable with clear
sections and bullet points.

(fork agent: general-purpose)
