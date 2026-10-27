# Releasing

One package, `noeta`, published to PyPI by `.github/workflows/release.yml` when a `vX.Y.Z` tag
is pushed. Pushing the tag is the maintainer's sign-off.

1. `CHANGELOG.md`: turn `## [Unreleased]` into `## [X.Y.Z] - YYYY-MM-DD` (the build job refuses a
   tag without a dated section) and complete it from `git log vPREV..HEAD`.
2. Set `version` in `pyproject.toml` to `X.Y.Z`; `uv lock`.
3. Merge to `main`, then confirm `main` carries the release commit and version
   (`git log -1`, `grep ^version pyproject.toml`) before tagging — merging and tagging are
   separate steps.
4. `git tag vX.Y.Z && git push origin vX.Y.Z`. The workflow runs `make check` on the tagged
   commit, builds, checks the wheel's version equals the tag, and publishes.
5. Verify: `uv pip install noeta==X.Y.Z` in a fresh venv (the index lags the publish by a minute
   or two).

If a run hangs on GitHub's side, start it again with
`gh workflow run release.yml --ref vX.Y.Z`; never move or re-push a tag.

PyPI uses Trusted Publishing: the `noeta` project on pypi.org has this repository's
`release.yml` registered as its publisher (no environment). No token is stored anywhere.
