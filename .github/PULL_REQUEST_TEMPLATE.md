## Summary

What this change does and why. Lead with the conclusion.

## Which package(s)

- [ ] `noeta-runtime` (engine / kernel / agent materials)
- [ ] `noeta-sdk` (in-process client surface)
- [ ] `noeta-agent` (app shell + web)
- [ ] docs

## Related ADR

If this changes a long-term decision, update the matching `docs/adr/` file and
`CONTEXT.md` in lockstep (see [CONTRIBUTING.md](../CONTRIBUTING.md)). Link the
ADR touched, or write "none — no decision changed".

## How verified

```bash
uv run pytest
uv run lint-imports --config .importlinter
uv run python scripts/lint-naming.py
```

Note anything that couldn't be verified and why.

## Checklist

- [ ] Tests pass (`uv run pytest`) and import/naming lints are clean.
- [ ] If the SDK public surface changed, the `examples/` still run (their smoke tests pass).
- [ ] If a decision changed, `docs/adr/` and `CONTEXT.md` were updated in lockstep.
