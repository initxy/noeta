## Summary

What this change does and why. Lead with the conclusion.

## Kind

- [ ] behavior (with a test that fails before it)
- [ ] structure only (no behavior change)
- [ ] docs

Structure and behavior never share a pull request (see [CONTRIBUTING.md](../CONTRIBUTING.md)).

## Spec / decision

Link the spec in `docs/specs/` this implements, or "none — no open decision". If a decision in
`docs/adr/` changes, link the new ADR that refines or supersedes it.

## How verified

```bash
make check   # lint, mypy, layering, tests with coverage, examples, doc hygiene
```

Paste the result. For model-facing text or provider wire changes, also `make eval` /
`make parity` and the relevant `make e2e` tests. Say what could not be verified and why.

## Checklist

- [ ] `make check` passes.
- [ ] Docs the change makes wrong are fixed in it (`CONTEXT.md`, `docs/`, `CHANGELOG.md` Unreleased).
- [ ] If this implements a spec: its status, Plan checkboxes and Progress log are up to date.
- [ ] A human owner has read this change and can answer review questions about it.
