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
make check   # pytest + coverage, mypy --strict on protocols, naming + import lints — mirrors CI
```

Note anything that couldn't be verified and why. The Postgres storage contract
tests, the web e2e smoke, and the install smoke run in CI only (see
[CONTRIBUTING.md](../CONTRIBUTING.md)).

## Checklist

- [ ] `make check` passes.
- [ ] If the SDK public surface changed, the `examples/` still run (their smoke tests pass).
- [ ] If a decision changed, `docs/adr/` and `CONTEXT.md` were updated in lockstep.
- [ ] A human owner has read this change and can answer review questions about it.
