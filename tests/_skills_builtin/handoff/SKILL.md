---
name: handoff
description: Compress the current session into a concise handoff document for another agent
argument-hint: [focus, e.g. "emphasize the open bug" or a target file path]
---

# Hand off the session

Distill everything that matters about the current task into a self-contained handoff
document, so a fresh agent with none of this conversation's context can pick up exactly
where you left off.

Extra focus or output target from the user (optional): $ARGUMENTS

## Steps

1. Recover the ground truth. Don't rely on memory alone — run `git_status` and
   `git_diff` to capture the actual in-progress changes, and `read` the key files
   you've been editing so the handoff reflects the real current state.
2. Write the handoff with these sections, in this order:
   - **Goal** — the task in one or two sentences: what we're trying to achieve and why.
   - **State** — what is already done and working, with the concrete files / functions
     touched (cite paths).
   - **In progress** — what is half-done right now, and where exactly the cursor is.
   - **Next steps** — the ordered, specific actions the next agent should take.
   - **Open questions / risks** — unresolved decisions, blockers, and known gotchas.
   - **How to verify** — the exact commands to build / test / run this work.
3. Be specific and load-bearing. Prefer file paths, command lines, function names, and
   exact error messages over vague summaries. Cut anything the next agent could
   rediscover trivially.
4. Emit the result. By default print the handoff as your final message. If the user
   asked for a file (or named a path above), write it there with `write_file`.

Capture only what you actually know from this session and the files you read — do not
speculate about parts of the codebase you never looked at.

(fork agent: main)
