# Releasing

`noeta-runtime` and `noeta-sdk` are published from one repo under one tag, but
they do **not** have to move together. A merged behavior change to
`packages/noeta-runtime` or `packages/noeta-sdk` should be followed by a
release — published packages must not lag `main`.

## What a tag publishes

One `vX.Y.Z` tag triggers `release.yml`. The build job builds both
distributions once — a wheel and an sdist for each of the two packages, four
files in total — and two publish jobs then run, one per package. **Each publish
job is gated on the tag version**: it uploads only if the build produced a wheel
whose version equals `X.Y.Z`, and otherwise skips with a notice.

The practical consequence: bump only the packages you are actually releasing.
The unbumped ones skip cleanly instead of failing on a duplicate upload. Both
shapes are supported and normal:

- **Lockstep** — bump both to `X.Y.Z`; both gates open.
- **Partial** — bump only what changed (e.g. a runtime-only fix leaves
  `noeta-sdk` at its current version); the held package's job skips.

Cross-package `>=` lower bounds are what keep a partial release coherent: a
bumped `noeta-sdk` must raise its `noeta-runtime>=` floor to the version
carrying the behavior it depends on.

## Version policy

- **Patch by default**: bug fixes, small additive API, packaging fixes.
- **Minor / major**: the maintainer's explicit call (feature-level or breaking
  release) — don't derive it mechanically from semver; ask.

## Procedure

1. Decide the scope: which of the two packages this release actually ships
   (see "What a tag publishes"). A package whose source did not change stays at
   its current version.
2. Update `CHANGELOG.md`: add a `## [X.Y.Z] - <date>` section with curated,
   user-visible changes only — not commit subjects — completed from
   `git log vPREV..HEAD`. Note which packages the release covers when it is a
   partial one, and add the `[X.Y.Z]` link definition at the bottom.
   `release.yml` refuses to publish a tag whose version has no dated changelog
   section (the exact pattern it greps for is
   `## [X.Y.Z] - YYYY-MM-DD`).
3. Bump `version` in each pyproject **in scope**, and raise the cross-package
   `>=` lower bound that must move with it (`noeta-sdk` →
   `noeta-runtime>=X.Y.Z`). Leave the out-of-scope package alone.
4. Run `uv sync` to refresh `uv.lock`.
5. Merge to `main` via PR with CI green.
6. `git tag vX.Y.Z && git push origin vX.Y.Z` — `release.yml` builds the wheels
   and sdists for both packages and publishes the in-scope ones via PyPI
   trusted publishing (no stored token).

## Verification

Install from PyPI into a clean venv with `uv pip install --no-cache
noeta-sdk==X.Y.Z` (the JSON API and simple index lag the publish by a minute
or two behind the CDN) and import the surface the release changed.

For a partial release, also check the Actions run: the in-scope publish jobs
should have uploaded and the out-of-scope ones should show the
`no <package>-X.Y.Z wheel — not part of this release; skipping` notice. A job
that skipped when you expected it to publish means its version bump was missed
in step 3.

## Notes

- Trusted-publisher environment mapping on pypi.org: `noeta-runtime` → (blank
  env), `noeta-sdk` → `pypi-sdk`. Each package's GitHub Actions environment is
  what makes its trusted-publisher config unique, which is why there are two
  publish jobs over one build.
- A module must ship in the wheel whose dependencies it imports.
  `tests/test_install_smoke.py::test_no_distribution_imports_outside_its_dependency_closure`
  enforces this statically — it is what catches "works in the checkout,
  `ModuleNotFoundError` for anyone who installs one package".
