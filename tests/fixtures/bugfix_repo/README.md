# bugfix_repo — Phase 4 I6 acceptance fixture

A tiny repo with one **known failing test**. Phase 4 I6's
deterministic bug-fixer CI gate (fake LLM) and the real-LLM
acceptance (gpt-5.5) both target this fixture: a successful run
finds + applies a one-line `edit` patch in
`src/math_ops.py` so `tests/test_add.py` flips from red to green.

The repo also carries `.noeta/skills/fix-python-test/SKILL.md` —
the runner pre-activates it via the I3/B17 durable event so
`ContextPlan.selected_skills` records that the skill body landed
in semi-stable context (auditable in `inspect` / the console SPA).

This is a **test fixture**, not a separately-packaged Python
project. Tests copy it to a `tmp_path` so the original tree is
never mutated.
