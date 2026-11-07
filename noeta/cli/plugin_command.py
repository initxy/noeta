"""``noeta plugin``: install plugins into ``~/.noeta/plugins``, pinned.

    noeta plugin add <git-url>@<commit>           a git repository at one commit
    noeta plugin add <path>                       a local plugin file or directory (copied)
    noeta plugin add <name>                       a plugin a configured marketplace lists
    noeta plugin add --marketplace <src> <name>   a plugin a Claude Code marketplace lists
    noeta plugin list                             what was installed this way, and from where
    noeta plugin configure <name>                 set a Claude Code plugin's userConfig values
    noeta plugin remove <name>
    noeta plugin marketplace add|list|remove      the marketplaces a bare name is looked up in

A plugin is a noeta plugin file or directory, or a Claude Code plugin
directory (``noeta.cli.claude_plugins``). A git source must name a full commit
(``@<sha>``): what is installed never changes under the person. A
marketplace (``<root>/.claude-plugin/marketplace.json``: a local path,
``<git-url>@<commit>``, or a git URL read at its default branch) is only
read, to find where its plugin comes from; its entry must pin a ``sha`` (or
give ``--commit``), and a path entry is pinned only by a marketplace that is
local or at a commit. Each entry may carry a ``noeta`` object (``ui_api``,
``parts``). Installing runs nothing from the plugin: no install scripts, no
git hooks, no dependency install. What was installed is recorded in
``~/.noeta/plugin-sources.json``, the configured marketplaces in
``~/.noeta/marketplaces.json`` (the official one until it is removed).

A Claude Code plugin whose manifest declares ``userConfig`` is configured as
it is added, as Claude Code does when a plugin is enabled: each value is
asked for on a terminal, or given with ``--config KEY=VALUE``
(:mod:`noeta.cli.plugin_options` says where they are kept).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import posixpath
import re
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from noeta.cli import home_dir, plugin_options

__all__ = ["MOVED_OUT", "add_parser", "install", "market_entries", "marketplaces",
           "missing_plugin_line", "run"]

log = logging.getLogger("noeta.cli")

SOURCES_FILE = "plugin-sources.json"
MARKETPLACES_FILE = "marketplaces.json"
#: Marketplaces read from a repository, kept between reads.
MARKET_CACHE = "marketplace-cache"
#: The official marketplace: configured until the person removes it.
OFFICIAL_MARKETPLACE = "noeta-plugins"
OFFICIAL_SOURCE = "https://github.com/initxy/noeta-plugins"
#: Seconds a marketplace read at its default branch is used before it is
#: read again (one at a commit never changes).
MARKET_TTL = 3600.0
#: Plugins that were part of noeta and now come from the official marketplace.
MOVED_OUT = frozenset({"repetition_guard"})
_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")
#: What may be meant as a commit after ``@`` (a short one is refused with a
#: reason: a commit made later could share its prefix).
_COMMIT = re.compile(r"^[0-9a-f]{4,64}$")
#: A full commit id: SHA-1, or SHA-256 for a repository in that format.
_FULL_COMMIT = re.compile(r"^([0-9a-f]{40}|[0-9a-f]{64})$")
#: Encrypted transports only (and local ``file://``): over ``http://`` or
#: ``git://`` anyone on the way could hand over other code.
_GIT_URL = re.compile(r"^(https://|ssh://|file://|[\w.-]+@[\w.-]+:)")


class PluginInstallError(Exception):
    pass


def add_parser(sub: Any) -> None:
    plugin = sub.add_parser("plugin", help="install, list and remove plugins (pinned)")
    cmds = plugin.add_subparsers(dest="plugin_command", required=True)
    add = cmds.add_parser("add", help="install a plugin from git (at a commit), a path or a "
                                      "marketplace")
    add.add_argument("source", help="<git-url>@<commit>, a path, or a plugin name a "
                                    "marketplace lists")
    add.add_argument("--marketplace", help="a configured marketplace's name, or a Claude Code "
                                           "marketplace: a path or a git URL (@<commit>)")
    add.add_argument("--commit", help="the commit to install when the marketplace entry "
                                      "pins none")
    configure = cmds.add_parser("configure", help="set a Claude Code plugin's userConfig "
                                                  "values")
    configure.add_argument("name")
    for parser in (add, configure):
        parser.add_argument("--config", action="append", default=[], metavar="KEY=VALUE",
                            help="a userConfig value (repeatable); the others are asked for "
                                 "on a terminal")
    cmds.add_parser("list", help="the plugins installed with noeta plugin add")
    remove = cmds.add_parser("remove", help="remove an installed plugin")
    remove.add_argument("name")
    market = cmds.add_parser("marketplace", help="the marketplaces plugin names are looked "
                                                 "up in")
    markets = market.add_subparsers(dest="market_command", required=True)
    markets.add_parser("add", help="add a marketplace").add_argument(
        "source", help="a path, <git-url>@<commit>, or a git URL (read at its default branch)")
    markets.add_parser("list", help="the marketplaces and the plugins they list")
    markets.add_parser("remove", help="remove a marketplace").add_argument("name")


def _split(spec: str) -> tuple[str, str | None]:
    """``source@commit`` as (source, commit); the part after the last
    ``@`` is a commit only when it looks like one."""
    source, at, rest = spec.rpartition("@")
    if at and _COMMIT.match(rest.lower()):
        return source, rest.lower()
    return spec, None


def _git(*args: str, cwd: str | None = None, timeout: float = 300) -> str:
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
    try:
        done = subprocess.run(["git", "-c", "core.hooksPath=/dev/null", "-c",
                               "protocol.ext.allow=never", *args],
                              cwd=cwd, env=env, capture_output=True, text=True,
                              timeout=timeout, check=False)
    except subprocess.TimeoutExpired as exc:
        raise PluginInstallError(f"git {args[0]} took longer than {timeout:.0f} s") from exc
    if done.returncode != 0:
        raise PluginInstallError(f"git {args[0]} failed: {done.stderr.strip()}")
    return done.stdout.strip()


def _git_url(url: str) -> None:
    """Refuse what does not look like a git URL over an encrypted transport
    (a marketplace's entry could otherwise smuggle in a git option)."""
    if not _GIT_URL.match(url):
        raise PluginInstallError(f"{url!r} is not a git URL over an encrypted transport "
                                 "(https://, ssh://, user@host:path, or a local file://)")


def _remote(source: str) -> bool:
    return bool(_GIT_URL.match(source)) or "://" in source  # another scheme: refused


def _checkout(url: str, commit: str, into: Path) -> str:
    """Clone ``url`` into ``into`` at ``commit`` (no submodules, no hooks);
    returns the full commit id."""
    _git_url(url)
    if not _FULL_COMMIT.match(commit):
        raise PluginInstallError(f"{commit!r} is not a full commit id (40 hex digits, or 64 "
                                 "in a SHA-256 repository): a short one could match a "
                                 "commit pushed later")
    _git("clone", "--quiet", "--no-checkout", "--", url, str(into))
    _git("checkout", "--quiet", "--detach", commit, cwd=str(into))
    full = _git("rev-parse", "HEAD", cwd=str(into))
    if full != commit:
        raise PluginInstallError(f"{url}@{commit} resolved to {full}")
    shutil.rmtree(into / ".git")
    return full


def _clone_branch(url: str, into: Path) -> str:
    """Clone the tip of ``url``'s default branch into ``into`` (a
    marketplace's listing: data, never installed as it is); returns its
    commit."""
    _git_url(url)
    _git("clone", "--quiet", "--depth", "1", "--no-tags", "--", url, str(into), timeout=120)
    full = _git("rev-parse", "HEAD", cwd=str(into))
    shutil.rmtree(into / ".git")
    return full


def _plugin_name(path: Path, listed: str | None = None) -> str:
    """The file's stem, else the manifest's ``name``, else the name a
    marketplace lists it by, else the directory's."""
    if path.is_file():
        return path.stem
    manifest = path / ".claude-plugin" / "plugin.json"
    if manifest.is_file():
        name = json.loads(manifest.read_text("utf-8")).get("name")
        if isinstance(name, str) and name:
            return name
    return listed or path.name.removesuffix(".git")


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


@dataclass(frozen=True)
class _Market:
    """A marketplace as read: its listing and how it was fetched."""

    root: Path
    data: dict[str, Any]
    #: ``path`` (a local directory), ``pinned`` (a repository at a commit)
    #: or ``branch`` (a repository's default branch, which can move).
    kind: str
    #: The commit read, for a repository.
    commit: str | None = None
    url: str = ""


def _read_market(source: str, work: Path) -> _Market:
    """The marketplace ``source``; a repository is fetched into ``work``."""
    url, commit = _split(source)
    read: str | None = None
    if _remote(url):
        root = work / "marketplace"
        kind = "pinned" if commit is not None else "branch"
        read = _checkout(url, commit, root) if commit is not None else _clone_branch(url, root)
    else:
        url, kind = "", "path"
        root = Path(source).expanduser().resolve()
        if root.is_file():  # <root>/.claude-plugin/marketplace.json
            root = root.parent.parent
    listing = root / ".claude-plugin" / "marketplace.json"
    if not listing.is_file():
        raise PluginInstallError(f"no .claude-plugin/marketplace.json in {source}")
    data = json.loads(listing.read_text("utf-8"))
    if not isinstance(data, dict) or not isinstance(data.get("plugins", []), list):
        raise PluginInstallError(f"{source}: its marketplace.json lists no plugins")
    return _Market(root, data, kind, read, url)


def _relative(data: dict[str, Any], src: str) -> str:
    """A path entry's place in its marketplace (``metadata.pluginRoot``
    applies to a bare name, as in Claude Code)."""
    plugin_root = (data.get("metadata") or {}).get("pluginRoot", ".")
    return src if src.startswith("./") or src == "." or "/" in src else f"{plugin_root}/{src}"


def _entry_url(src: dict[str, Any]) -> str | None:
    """The repository a ``github``, ``url`` or ``git-subdir`` source names;
    ``None`` for another kind."""
    kind = src.get("source")
    if kind == "github":
        return f"https://github.com/{src.get('repo')}.git"
    if kind in ("url", "git-subdir"):
        url = str(src.get("url") or "")
        if kind == "git-subdir" and re.fullmatch(r"[\w.-]+/[\w.-]+", url):
            url = f"https://github.com/{url}.git"
        return url
    return None


def _from_marketplace(market: str, name: str, commit: str | None,
                      work: Path) -> tuple[Path, str | None]:
    """Where the marketplace ``market`` says ``name`` comes from, fetched
    into ``work``, and the commit it is at; records nothing, runs nothing.
    A ``commit`` other than the one the entry pins is refused (the person
    was shown the entry's)."""
    m = _read_market(market, work)
    entry = next((p for p in m.data.get("plugins") or []
                  if isinstance(p, dict) and p.get("name") == name), None)
    if entry is None:
        names = ", ".join(str(p.get("name")) for p in m.data.get("plugins") or []
                          if isinstance(p, dict))
        raise PluginInstallError(f"the marketplace lists no plugin {name!r} (it has: {names})")
    src = entry.get("source")
    if isinstance(src, str):
        if m.kind == "branch":
            raise PluginInstallError(f"plugin {name!r} pins no commit: it is a path in a "
                                     "marketplace read at its default branch, which can move; "
                                     "add the marketplace as <git-url>@<commit>")
        if commit and m.commit and commit.lower() != m.commit:
            raise PluginInstallError(f"plugin {name!r} is at {m.commit} in that marketplace, "
                                     f"not {commit}")
        return _inside(m.root, _relative(m.data, src)), m.commit
    if not isinstance(src, dict):
        raise PluginInstallError(f"plugin {name!r}: unreadable source")
    url = _entry_url(src)
    if url is None:
        raise PluginInstallError(f"plugin {name!r}: a {src.get('source')!r} source is not "
                                 "supported (github, url, git-subdir or a path are)")
    pinned = str(src.get("sha") or "").lower()
    if pinned and commit and commit.lower() != pinned:
        raise PluginInstallError(f"plugin {name!r}: the marketplace pins {pinned}, not {commit}")
    sha = pinned or str(commit or "").lower()
    if not _COMMIT.match(sha):
        ref = f" (its ref {src['ref']!r} is not followed: a branch or tag can move)" \
            if src.get("ref") else ""
        raise PluginInstallError(f"plugin {name!r} pins no commit: its entry has no sha{ref}; "
                                 "give --commit")
    fetched = work / "plugin"
    full = _checkout(url, sha, fetched)
    if src.get("source") == "git-subdir":
        return _inside(fetched, str(src.get("path") or ".")), full
    return fetched, full


def marketplaces() -> dict[str, str]:
    """The configured marketplaces, name -> source: the official one until
    the person changes the list."""
    path = home_dir.HOME / MARKETPLACES_FILE
    if not path.is_file():
        return {OFFICIAL_MARKETPLACE: OFFICIAL_SOURCE}
    data = json.loads(path.read_text("utf-8"))
    table = data.get("marketplaces") if isinstance(data, dict) else None
    return {str(name): str(item["source"]) for name, item in (table or {}).items()
            if isinstance(item, dict) and isinstance(item.get("source"), str)}


def _save_marketplaces(table: dict[str, str]) -> None:
    home_dir.ensure_home()
    data = {"marketplaces": {name: {"source": source} for name, source in table.items()}}
    (home_dir.HOME / MARKETPLACES_FILE).write_text(json.dumps(data, indent=2) + "\n", "utf-8")


def _cache_file(source: str) -> Path:
    digest = hashlib.sha256(source.encode()).hexdigest()[:16]
    return home_dir.HOME / MARKET_CACHE / f"{digest}.json"


def _listing(source: str, *, refresh: bool = False) -> _Market:
    """The marketplace ``source`` as read now, or as kept from a read
    within :data:`MARKET_TTL` (for one at a commit, any read)."""
    cache = _cache_file(source)
    remote = _remote(_split(source)[0])
    if remote and not refresh and cache.is_file():
        try:
            kept = json.loads(cache.read_text("utf-8"))
            if kept["source"] == source and (kept["kind"] == "pinned"
                                             or time.time() - kept["read_at"] < MARKET_TTL):
                return _Market(Path(kept["root"]), kept["data"], kept["kind"], kept["commit"],
                               kept["url"])
        except (ValueError, KeyError, TypeError):
            pass  # read it again
    with tempfile.TemporaryDirectory(prefix="noeta-marketplace-") as tmp:
        m = _read_market(source, Path(tmp))
    if remote:
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_text(json.dumps({"source": source, "read_at": time.time(), "kind": m.kind,
                                     "commit": m.commit, "url": m.url, "root": str(m.root),
                                     "data": m.data}), "utf-8")
    return m


def _installed_names() -> set[str]:
    plugins = home_dir.HOME / "plugins"
    found = {p.stem if p.suffix == ".py" else p.name
             for p in (plugins.iterdir() if plugins.is_dir() else ())}
    return found | set(_records())


def _market_entry(market: str, m: _Market, entry: dict[str, Any],
                  installed: set[str]) -> dict[str, Any]:
    """One MarketEntry: what a person is shown before installing it."""
    src = entry.get("source")
    commit: str | None = None
    if isinstance(src, str):
        relative = posixpath.normpath(_relative(m.data, src))
        source = str(m.root / relative) if m.kind == "path" else f"{m.url}#{relative}"
        commit = m.commit if m.kind == "pinned" else None
    elif isinstance(src, dict):
        url = _entry_url(src)
        source = f"a {src.get('source')!r} source (not supported)" if url is None else \
            f"{url}#{src.get('path') or '.'}" if src.get("source") == "git-subdir" else url
        commit = str(src["sha"]).lower() if src.get("sha") else None
    else:
        source = "(unreadable)"
    extra = entry["noeta"] if isinstance(entry.get("noeta"), dict) else {}
    parts, ui_api = extra.get("parts"), extra.get("ui_api")
    name = str(entry["name"])
    return {"marketplace": market, "name": name,
            "description": str(entry.get("description") or ""),
            "version": str(entry.get("version") or ""), "source": source, "commit": commit,
            "parts": [p for p in parts if isinstance(p, str)] if isinstance(parts, list) else [],
            "ui_api": ui_api if isinstance(ui_api, int) and not isinstance(ui_api, bool)
            else None,
            "installed": name in installed}


def market_entries(notices: list[str] | None = None, *,
                   refresh: bool = False) -> list[dict[str, Any]]:
    """Every plugin the configured marketplaces list, as MarketEntry dicts:
    ``marketplace``, ``name``, ``description``, ``version``, ``source``,
    ``commit`` (its pin, ``None`` for an entry that pins none), ``parts``
    and ``ui_api`` (from the entry's ``noeta`` object), ``installed``. A
    marketplace that cannot be read is left out with a line in ``notices``
    (in the log when none is given)."""
    installed = _installed_names()
    out: list[dict[str, Any]] = []
    for market, source in marketplaces().items():
        try:
            m = _listing(source, refresh=refresh)
        except (PluginInstallError, OSError, ValueError) as exc:
            text = f"marketplace {market} ({source}) could not be read: {exc}"
            if notices is None:
                log.warning("%s", text)
            else:
                notices.append(text)
            continue
        out += [_market_entry(market, m, entry, installed)
                for entry in m.data.get("plugins") or []
                if isinstance(entry, dict) and isinstance(entry.get("name"), str)
                and entry["name"]]
    return out


def missing_plugin_line(name: str) -> str:
    """What to tell the person about a plugin a setting names that is not
    installed here."""
    if name in MOVED_OUT:
        return (f"{name} is no longer part of noeta; it is in the {OFFICIAL_MARKETPLACE} "
                f"marketplace: noeta plugin add {name}")
    return f"no plugin {name} is installed here (noeta plugins lists them)"


def _bare_name(spec: str) -> bool:
    return not _remote(spec) and bool(_NAME.fullmatch(spec)) and not Path(spec).exists()


def _resolve(name: str) -> str:
    """The one configured marketplace that lists ``name``."""
    notices: list[str] = []
    found = sorted({str(e["marketplace"]) for e in market_entries(notices) if e["name"] == name})
    if len(found) > 1:
        raise PluginInstallError(f"several marketplaces list {name!r} ({', '.join(found)}); "
                                 f"name one: noeta plugin add {name} --marketplace <name>")
    if not found:
        why = f" ({'; '.join(notices)})" if notices else ""
        raise PluginInstallError(f"{name}: no such file or directory, and no configured "
                                 f"marketplace lists a plugin {name!r}{why}")
    return found[0]


def install(spec: str, *, marketplace: str | None = None,
            commit: str | None = None) -> tuple[str, Path]:
    """Install a plugin (see the module docstring); returns its name and
    where it now is."""
    if marketplace is None and _bare_name(spec):
        marketplace = _resolve(spec)
    with tempfile.TemporaryDirectory(prefix="noeta-plugin-") as tmp:
        work = Path(tmp)
        if marketplace is not None:
            # A configured marketplace by its name, else a path or a URL.
            market = marketplaces().get(marketplace, marketplace)
            found, at = _from_marketplace(market, spec, commit, work)
            name = _plugin_name(found, spec) if found.is_dir() else spec
            record: dict[str, Any] = {"marketplace": marketplace, "plugin": spec,
                                      **({"commit": at} if at else {})}
        else:
            source, pinned = _split(spec)
            pinned = pinned or (commit.lower() if commit else None)
            if _remote(source):
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


def _fields(path: Path) -> dict[str, dict[str, Any]]:
    """The ``userConfig`` fields of the plugin at ``path``."""
    manifest = path / ".claude-plugin" / "plugin.json"
    if not manifest.is_file():
        return {}
    data = json.loads(manifest.read_text("utf-8"))
    return plugin_options.schema(data) if isinstance(data, dict) else {}


def _installed(name: str) -> Path | None:
    rec = _records().get(name)
    plugins = home_dir.HOME / "plugins"
    for path in ([Path(rec["path"])] if rec and rec.get("path") else []) + [plugins / name]:
        if path.is_dir():
            return path
    return None


def _configure(name: str, path: Path, given: list[str]) -> int:
    fields = _fields(path)
    try:
        notes = plugin_options.configure(name, fields, given)
    except plugin_options.OptionError as exc:
        print(f"noeta: {exc}", file=sys.stderr)
        return 1
    for note in notes:
        print(f"noeta: {note}", file=sys.stderr)
    return 0


def _remove(name: str, *, quiet: bool = False, keep_options: bool = False) -> int:
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
    if not keep_options:
        plugin_options.forget(name)
    if quiet:
        return 0
    if not removed and rec is None:
        print(f"noeta: no plugin {name} in {plugins}", file=sys.stderr)
        return 1
    print(f"removed {name}; running front ends pick it up with /reload-plugins")
    return 0


def _marketplace_add(source: str) -> int:
    if not _remote(_split(source)[0]):
        source = str(Path(source).expanduser().resolve())
    try:
        m = _listing(source, refresh=True)
        name = m.data.get("name")
        if not isinstance(name, str) or not _NAME.fullmatch(name):
            raise PluginInstallError(f"{source}: its marketplace.json has no usable name")
    except (PluginInstallError, OSError, ValueError) as exc:
        print(f"noeta: {exc}", file=sys.stderr)
        return 1
    table = marketplaces()
    before = table.get(name)
    table[name] = source
    _save_marketplaces(table)
    if before not in (None, source):
        print(f"marketplace {name} now reads {source} (it read {before})")
    else:
        print(f"added marketplace {name} from {source} "
              f"({len(m.data.get('plugins') or [])} plugins)")
    return 0


def _marketplace_list() -> int:
    table = marketplaces()
    if not table:
        print("no marketplaces (noeta plugin marketplace add <path | git-url[@commit]>)")
    notices: list[str] = []
    entries = market_entries(notices)
    for name, source in table.items():
        print(f"{name}\t{source}")
        for e in (e for e in entries if e["marketplace"] == name):
            installed = "\tinstalled" if e["installed"] else ""
            print(f"  {e['name']}\t{e['version'] or '-'}\t"
                  f"{e['commit'] or '(pins no commit)'}{installed}")
    for notice in notices:
        print(f"noeta: {notice}", file=sys.stderr)
    return 0


def _marketplace_remove(name: str) -> int:
    table = marketplaces()
    source = table.pop(name, None)
    if source is None:
        print(f"noeta: no marketplace {name} (noeta plugin marketplace list)", file=sys.stderr)
        return 1
    _save_marketplaces(table)
    _cache_file(source).unlink(missing_ok=True)
    print(f"removed marketplace {name}; the plugins installed from it stay")
    return 0


def run(args: argparse.Namespace, cwd: str) -> int:
    """Run ``noeta plugin <command>``; the exit status."""
    if args.plugin_command == "marketplace":
        if args.market_command == "add":
            return _marketplace_add(args.source)
        if args.market_command == "list":
            return _marketplace_list()
        return _marketplace_remove(args.name)
    if args.plugin_command == "list":
        return _list()
    if args.plugin_command == "remove":
        return _remove(args.name)
    if args.plugin_command == "configure":
        path = _installed(args.name)
        if path is None or not _fields(path):
            print(f"noeta: {args.name} is no installed Claude Code plugin with userConfig "
                  "options", file=sys.stderr)
            return 1
        return _configure(args.name, path, args.config)
    try:
        name, path = install(args.source, marketplace=args.marketplace, commit=args.commit)
    except (PluginInstallError, OSError, ValueError) as exc:
        print(f"noeta: {exc}", file=sys.stderr)
        return 1
    if path.is_dir() and _fields(path):
        if _configure(name, path, args.config):
            # Not left installed half configured; options kept from before
            # stay (a failed configure writes none).
            _remove(name, quiet=True, keep_options=True)
            print(f"noeta: {name} was not installed; add it again once its options can be "
                  "kept (--config KEY={env:NAME} keeps a secret in a variable)",
                  file=sys.stderr)
            return 1
    elif args.config:
        _remove(name, quiet=True, keep_options=True)
        print(f"noeta: --config was given but plugin {name!r} declares no userConfig options; "
              "not installed", file=sys.stderr)
        return 1
    print(f"installed {name} in {path}; it loads in front ends started from now "
          "(or /reload-plugins)")
    return 0
