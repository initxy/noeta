# noeta-tui

noeta's terminal UI, shaped after Claude Code's: `noeta` with no arguments opens it.
It is a front end like any other: it runs on noeta's `AppAPI` (`noeta/host/app.py`), a
`LocalApp` in process or a `RemoteApp` against `noeta serve` (`noeta --server URL`), and
imports no plugin.

## Install

```bash
uv tool install 'noeta[tui]'     # or: pip install 'noeta[tui]'
# from a checkout of the repository:  uv tool install '.[tui]'
```

It is released with `noeta`, always at the same version, and the `tui` extra pins it. `noeta` finds it through the
entry-point group `noeta.frontends` (name `tui`) and prints the install line when it is
missing. Documentation: <https://initxy.github.io/noeta/guides/terminal>.

## Develop

It is a member of the repository's uv workspace. From the repository root:

```bash
make tui        # its lint, types and tests (also part of make check)
```

`screenshots/` holds what it looks like next to Claude Code ([screenshots/README.md](https://github.com/initxy/noeta/blob/main/tui/screenshots/README.md)).
