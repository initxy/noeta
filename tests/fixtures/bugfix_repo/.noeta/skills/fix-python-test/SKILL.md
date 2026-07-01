---
name: fix-python-test
description: minimal-patch loop for a failing pytest in a python project
priority: 50
---

When a Python test is failing, follow this loop precisely:

1. Run `pytest -q` via `shell_run` and read the tail of the output.
   The tail names the failing test + the offending line.
2. Use `grep` to find the function under test in the source tree.
   Read just the relevant lines of that file with `read`.
3. Apply the smallest possible patch with `edit` —
   `old` MUST match the file exactly once. Prefer changing a single
   operator or literal over rewriting a function.
4. Rerun `pytest -q` to confirm the suite is green. If a test
   still fails, go back to step 2 — do NOT pile more `edit`
   calls on top.
