"""``noeta plugin``: install plugins into ``~/.noeta/plugins``, pinned.

    noeta plugin add <git-url>@<commit>           a git repository at one commit
    noeta plugin add <path>                       a local plugin file or directory (copied)
    noeta plugin add --marketplace <src> <name>   a plugin a Claude Code marketplace lists
    noeta plugin list                             what was installed this way, and from where
    noeta plugin remove <name>

A plugin is a noeta plugin file or directory, or a Claude Code plugin
directory (``noeta.cli.claude_plugins``). A git source must name a commit
(``@<sha>``): what is installed never changes under the person. A
marketplace (``<root>/.claude-plugin/marketplace.json``, a local path or
``<git-url>@<commit>``) is only read, to find where its plugin comes from;
its entry must pin a ``sha`` (or give ``--commit``). Installing runs nothing
from the plugin: no install scripts, no git hooks, no dependency install.
What was installed is recorded in ``~/.noeta/plugin-sources.json``.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

from noeta.cli import home_dir

__all__ = ["add_parser", "install", "run"]

SOURCES_FILE = "plugin-sources.json"
_COMMIT = re.compile(r"^[0-9a-f]{7,40}$")
_GIT_URL = re.compile(r"^(https?://|ssh://|git://|file://|[\w.-]+@[\w.-]+:)")


class PluginInstallError(Exception):
    pass


def add_parser(sub: Any) -> None:
    plugin = sub.add_parser("plugin", help="install, list and remove plugins (pinned)")
    cmds = plugin.add_subparsers(dest="plugin_command", required=True)
    add = cmds.add_parser("add", help="install a plugin from git (at a commit) or a path")
    add.add_argument("source", help="<git-url>@<commit>, a path, or with --marketplace a "
                                    "plugin name")
    add.add_argument("--marketplace", help="a Claude Code marketplace: a path or "
                                           "<git-url>@<commit>")
    add.add_argument("--commit", help="the commit to install when the marketplace entry "
                                      "pins none")
    cmds.add_parser("list", help="the plugins installed with noeta plugin add")
    remove = cmds.add_parser("remove", help="remove an installed plugin")
    remove.add_argument("name")


def _split(spec: str) -> tuple[str, str | None]:
    """``source@commit`` as (source, commit); the part after the last
    ``@`` is a commit only when it looks like one."""
    source, at, rest = spec.rpartition("@")
    if at and _COMMIT.match(rest.lower()):
        return source, rest.lower()
    return spec, None


def _git(*args: str, cwd: str | None = None) -> str:
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
    done = subprocess.run(["git", "-c", "core.hooksPath=/dev/null", "-c",
                           "protocol.ext.allow=never", *args],
                          cwd=cwd, env=env, capture_output=True, text=True, timeout=300,
                          check=False)
    if done.returncode != 0:
        raise PluginInstallError(f"git {args[0]} failed: {done.stderr.strip()}")
    return done.stdout.strip()


def _checkout(url: str, commit: str, into: Path) -> str:
    """Clone ``url`` into ``into`` at ``commit`` (no submodules, no hooks);
    returns the full commit id. ``url`` must look like a git URL (a
    marketplace's entry could otherwise smuggle in a git option)."""
    if not _GIT_URL.match(url):
        raise PluginInstallError(f"{url!r} is not a git URL (https://, ssh://, git://, "
                                 "file:// or user@host:path)")
    if not _COMMIT.match(commit):
        raise PluginInstallError(f"{commit!r} is not a commit id")
    _git("clone", "--quiet", "--no-checkout", "--", url, str(into))
    _git("checkout", "--quiet", "--detach", commit, cwd=str(into))
    full = _git("rev-parse", "HEAD", cwd=str(into))
    if not full.startswith(commit):
        raise PluginInstallError(f"{url}@{commit} resolved to {full}")
    shutil.rmtree(into / ".git")
    return full


def _plugin_name(path: Path) -> str:
    if path.is_file():
        return path.stem
    manifest = path / ".claude-plugin" / "plugin.json"
    if manifest.is_file():
        name = json.loads(manifest.read_text("utf-8")).get("name")
        if isinstance(name, str) and name:
            return name
    return path.name.removesuffix(".git")


def _records() -> dict[str, Any]:
    path = home_dir.HOME / SOURCES_FILE
    data = json.loads(path.read_text("utf-8")) if path.is_file() else {}
    return data if isinstance(data, dict) else {}


def _save(records: dict[str, Any]) -> None:
    home_dir.ensure_home()
    (home_dir.HOME / SOURCES_FILE).write_text(json.dumps(records, indent=2, sort_keys=True) + "\n",
                                            "utf-8")


def _place(found: Path, name: str, record: dict[str, Any]) -> Path:
    """Copy ``found`` (a file or directory) into ``~/.noeta/plugins`` as
    ``name`` and record where it came from."""
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", name):
        raise PluginInstallError(f"{name!r} is not a usable plugin name")
    plugins = home_dir.HOME / "plugins"
    target = plugins / (f"{name}.py" if found.is_file() else name)
    if target.exists() or (plugins / name).exists() or (plugins / f"{name}.py").exists():
        raise PluginInstallError(f"{name} is already in {plugins}; noeta plugin remove {name} "
                                 "first")
    plugins.mkdir(parents=True, exist_ok=True)
    if found.is_file():
        shutil.copy2(found, target)
    else:
        shutil.copytree(found, target, symlinks=True,
                        ignore=shutil.ignore_patterns(".git", "__pycache__"))
    records = _records()
    records[name] = {**record, "path": str(target)}
    _save(records)
    return target


def _inside(root: Path, sub: str) -> Path:
    path = (root / sub).resolve()
    if path != root.resolve() and root.resolve() not in path.parents:
        raise PluginInstallError(f"{sub} is outside {root}")
    return path


def _from_marketplace(market: str, name: str, commit: str | None, work: Path) -> Path:
    """Where the marketplace ``market`` says ``name`` comes from, fetched
    into ``work``; records nothing, runs nothing."""
    source, market_commit = _split(market)
    if _GIT_URL.match(source):
        if market_commit is None:
            raise PluginInstallError("a git marketplace needs @<commit>")
        root = work / "marketplace"
        _checkout(source, market_commit, root)
    else:
        root = Path(source).expanduser().resolve()
        if root.is_file():  # <root>/.claude-plugin/marketplace.json
            root = root.parent.parent
    listing = root / ".claude-plugin" / "marketplace.json"
    if not listing.is_file():
        raise PluginInstallError(f"no .claude-plugin/marketplace.json in {root}")
    data = json.loads(listing.read_text("utf-8"))
    entry = next((p for p in data.get("plugins") or [] if p.get("name") == name), None)
    if entry is None:
        names = ", ".join(str(p.get("name")) for p in data.get("plugins") or [])
        raise PluginInstallError(f"the marketplace lists no plugin {name!r} (it has: {names})")
    src = entry.get("source")
    if isinstance(src, str):
        plugin_root = (data.get("metadata") or {}).get("pluginRoot", ".")
        relative = src if src.startswith("./") or src == "." or "/" in src \
            else f"{plugin_root}/{src}"
        return _inside(root, relative)
    if not isinstance(src, dict):
        raise PluginInstallError(f"plugin {name!r}: unreadable source")
    kind = src.get("source")
    if kind == "github":
        url = f"https://github.com/{src.get('repo')}.git"
    elif kind in ("url", "git-subdir"):
        url = str(src.get("url") or "")
        if kind == "git-subdir" and re.fullmatch(r"[\w.-]+/[\w.-]+", url):
            url = f"https://github.com/{url}.git"
    else:
        raise PluginInstallError(f"plugin {name!r}: a {kind!r} source is not supported "
                                 "(github, url, git-subdir or a path are)")
    sha = str(src.get("sha") or commit or "").lower()
    if not _COMMIT.match(sha):
        raise PluginInstallError(f"plugin {name!r} pins no commit: its entry has no sha; "
                                 "give --commit")
    fetched = work / "plugin"
    _checkout(url, sha, fetched)
    return _inside(fetched, str(src.get("path") or ".")) if kind == "git-subdir" else fetched


def install(spec: str, *, marketplace: str | None = None,
            commit: str | None = None) -> tuple[str, Path]:
    """Install a plugin (see the module docstring); returns its name and
    where it now is."""
    with tempfile.TemporaryDirectory(prefix="noeta-plugin-") as tmp:
        work = Path(tmp)
        if marketplace is not None:
            found = _from_marketplace(marketplace, spec, commit, work)
            name = _plugin_name(found) if found.is_dir() else spec
            record: dict[str, Any] = {"marketplace": marketplace, "plugin": spec}
        else:
            source, pinned = _split(spec)
            pinned = pinned or commit
            if _GIT_URL.match(source):
                if pinned is None:
                    raise PluginInstallError("a git source needs @<commit> (a pinned "
                                             "install never changes under you)")
                found = work / re.sub(r"\.git$", "", source.rstrip("/").rsplit("/", 1)[-1]
                                      .rsplit(":", 1)[-1])
                record = {"source": source, "commit": _checkout(source, pinned, found)}
            else:
                found = Path(source).expanduser().resolve()
                if not found.exists():
                    raise PluginInstallError(f"{source}: no such file or directory")
                record = {"source": str(found)}
            name = _plugin_name(found)
        return name, _place(found, name, record)


def _list() -> int:
    records = _records()
    if not records:
        print("no plugins installed with noeta plugin add (noeta plugins lists every plugin)")
    for name, rec in sorted(records.items()):
        origin = rec.get("source") or f"{rec.get('plugin')} from {rec.get('marketplace')}"
        commit = f"@{rec['commit']}" if rec.get("commit") else ""
        print(f"{name}\t{origin}{commit}\t{rec.get('path')}")
    return 0


def _remove(name: str) -> int:
    records = _records()
    plugins = (home_dir.HOME / "plugins").resolve()
    rec = records.pop(name, None)
    candidates = [Path(rec["path"])] if rec and rec.get("path") else \
        [plugins / name, plugins / f"{name}.py"]
    removed = False
    for path in candidates:
        if path.resolve().parent != plugins or not path.exists():
            continue
        shutil.rmtree(path) if path.is_dir() else path.unlink()
        removed = True
    _save(records)
    if not removed and rec is None:
        print(f"noeta: no plugin {name} in {plugins}", file=sys.stderr)
        return 1
    print(f"removed {name}; running front ends pick it up with /reload-plugins")
    return 0


def run(args: argparse.Namespace, cwd: str) -> int:
    """Run ``noeta plugin <command>``; the exit status."""
    if args.plugin_command == "list":
        return _list()
    if args.plugin_command == "remove":
        return _remove(args.name)
    try:
        name, path = install(args.source, marketplace=args.marketplace, commit=args.commit)
    except (PluginInstallError, OSError, ValueError) as exc:
        print(f"noeta: {exc}", file=sys.stderr)
        return 1
    print(f"installed {name} in {path}; it loads in front ends started from now "
          "(or /reload-plugins)")
    return 0
