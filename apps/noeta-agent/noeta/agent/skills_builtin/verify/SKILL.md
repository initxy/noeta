---
name: verify
description: Verify a change actually does what it claims by running it and observing behavior
argument-hint: [what to verify, e.g. "the new login retry logic"]
---

# Verify a change

Confirm that a code change truly does what it's supposed to — by running it and
observing real behavior, not just by reading the diff.

What to verify (the claim/feature under test): $ARGUMENTS

## Steps

1. Pin down the claim. Run `git_diff` and `read` on the touched code to state, in
   one sentence, what behavior this change is supposed to produce.
2. Find how this project runs and tests. `read` the manifest / `Makefile` / CI
   config and `grep` for existing test commands so you use the project's real entry
   points rather than inventing them.
3. Exercise it with `shell_run`:
   - Run the relevant automated tests (whole suite or a targeted single test) and read
     the output.
   - If behavior is observable another way (a CLI command, a script, a smoke run),
     execute that too and capture the actual output.
4. Compare observed behavior against the claim from step 1. Cover the happy path and at
   least one edge / failure case the change should handle.
5. Report a clear verdict: **verified** or **not verified**. Quote the exact command(s)
   you ran and the output you observed. If it fails, point to the smallest reproducing
   command and the precise mismatch between expected and actual.

Do not "fix" the code here — only verify and report. If you had to change anything to
make it runnable, call that out explicitly.

(fork agent: general-purpose)
