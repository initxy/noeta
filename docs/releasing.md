# Releasing

Two packages, `noeta` and the terminal UI `noeta-tui` (`tui/`), published to PyPI, and the two
web packages `@initxy/noeta-web-sdk` (`web/sdk/`) and `@initxy/noeta-ui` (`web/ui/`), published to
npm, always at the same version, by `.github/workflows/release.yml` when a `vX.Y.Z` tag is
pushed. Pushing the tag is the maintainer's sign-off.

1. `CHANGELOG.md`: turn `## [Unreleased]` into `## [X.Y.Z] - YYYY-MM-DD` (the build job refuses a
   tag without a dated section) and complete it from `git log vPREV..HEAD`.
2. Set `X.Y.Z` in four places: `version` in `pyproject.toml` and `tui/pyproject.toml`, the
   `tui` extra's `noeta-tui==` pin, and `noeta-tui`'s `noeta==` pin; then `uv lock`. In `web/`:
   `version` in `sdk/package.json` and `ui/package.json`, the `@initxy/` pins in `ui/package.json`
   and `package.json`, then `npm install` there (the lock follows).
   `tests/test_release_lockstep.py` fails if they disagree.
3. Merge to `main`, then confirm `main` carries the release commit and version
   (`git log -1`, `grep ^version pyproject.toml`) before tagging — merging and tagging are
   separate steps.
4. `git tag vX.Y.Z && git push origin vX.Y.Z`. The workflow runs `make check` on the tagged
   commit, builds both packages, checks both wheels' versions equal the tag and that they install
   together, publishes both, publishes the two web packages to npm, then creates the GitHub
   Release with that version's CHANGELOG section as its notes.
5. Verify: `uv pip install 'noeta[tui]==X.Y.Z'` in a fresh venv (the index lags the publish by a minute
   or two).

If a run hangs on GitHub's side, start it again with
`gh workflow run release.yml --ref vX.Y.Z`; never move or re-push a tag.

PyPI and npm use Trusted Publishing: the `noeta` and `noeta-tui` projects on pypi.org and the two
`@initxy/` packages on npmjs.com each have this repository's `release.yml` registered as their
publisher (no environment). No token is stored anywhere. A version no tag put on npm is published
by running the workflow by hand on `main` (`gh workflow run release.yml --ref main`).
