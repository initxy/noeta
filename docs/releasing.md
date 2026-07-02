# Releasing

`noeta-runtime` / `noeta-sdk` / `noeta-agent` share one version and always
release together. A merged behavior change to `packages/noeta-runtime`,
`packages/noeta-sdk`, or `apps/noeta-agent` should be followed by a release —
published packages must not lag `main`.

## Version policy

- **Patch by default**: bug fixes, small additive API, packaging fixes.
- **Minor / major**: the maintainer's explicit call (feature-level or breaking
  release) — don't derive it mechanically from semver; ask.

## Procedure

1. Bump `version` in all three member pyprojects **and** the lockstep `>=`
   cross-package lower bounds to the same value (`noeta-sdk` →
   `noeta-runtime>=X.Y.Z`; `noeta-agent` → both).
2. Update the version assertion in `tests/test_install_smoke.py`
   (`test_pyproject_metadata_is_present`).
3. Run `uv sync` to refresh `uv.lock`.
4. Merge to `main` via PR with CI green.
5. `git tag vX.Y.Z && git push origin vX.Y.Z` — `release.yml` builds the
   frontend + all wheels and publishes via PyPI trusted publishing (no stored
   token).

## Verification

Install from PyPI into a clean venv with `uv pip install --no-cache
noeta-sdk==X.Y.Z` (the JSON API and simple index lag the publish by a minute
or two behind the CDN) and import the surface the release changed.

## Notes

- `noeta-agent` is **wheel-only**: its wheel force-includes `../web/*`, which
  an sdist can't reach. Building locally, use `uv build --all-packages
  --wheel` — never a plain `uv build`.
- Trusted-publisher environment mapping on pypi.org: runtime → (blank env),
  sdk → `pypi-sdk`, agent → `pypi-agent`.
