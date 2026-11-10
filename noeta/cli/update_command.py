"""``noeta update``: upgrade noeta in place to the latest release on PyPI,
keeping what it was installed with.

* a uv tool install (``uv-receipt.toml`` in its environment): ``uv tool install
  --reinstall`` with the receipt's extras, ``--with`` packages, Python and index,
  noeta's version pin dropped (a reinstall keeps only what it names);
* a pip install: ``pip install -U 'noeta[<the extras installed>]'``;
* a source checkout or an editable install: the commands to run, nothing changed.

A running ``noeta web`` server is stopped first and started again after."""

from __future__ import annotations

import argparse
import importlib.metadata
import importlib.util
import json
import re
import shlex
import shutil
import subprocess
import sys
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from noeta.cli import web_command

PYPI_JSON = "https://pypi.org/pypi/noeta/json"


def add_parser(sub: Any) -> None:
    sub.add_parser("update", help="upgrade noeta to the latest release, keeping its extras")


def latest_version(timeout: float = 15.0) -> str:
    """The latest version of noeta on PyPI (proxies from the environment,
    as httpx reads them). Raises ``httpx.HTTPError`` or ``ValueError``."""
    import httpx

    resp = httpx.get(PYPI_JSON, timeout=timeout, follow_redirects=True)
    resp.raise_for_status()
    version = resp.json().get("info", {}).get("version")
    if not isinstance(version, str) or not version:
        raise ValueError("PyPI's answer names no version")
    return version


def _newer(latest: str, installed: str) -> bool:
    """Whether ``latest`` is a later release than ``installed`` (numeric
    parts compared; anything else only by equality)."""
    def parts(v: str) -> tuple[int, ...] | None:
        nums = v.split(".")
        return tuple(int(n) for n in nums) if all(n.isdigit() for n in nums) else None

    a, b = parts(latest), parts(installed)
    return a > b if a is not None and b is not None else latest != installed


def _distribution() -> importlib.metadata.Distribution | None:
    try:
        return importlib.metadata.distribution("noeta")
    except importlib.metadata.PackageNotFoundError:
        return None


def direct_url() -> dict[str, Any] | None:
    """Where noeta was installed from when that is source (PEP 610's
    ``direct_url.json`` naming a directory, editable or not, or a git
    repository), else ``None`` (an index, or a wheel or sdist file, which
    an update replaces from PyPI). ``{}`` when no installed distribution is
    found at all (running from a checkout on ``sys.path``)."""
    dist = _distribution()
    if dist is None:
        return {}
    try:
        found = json.loads(dist.read_text("direct_url.json") or "null")
    except ValueError:
        return {}
    if not isinstance(found, dict):
        return None
    return found if "dir_info" in found or "vcs_info" in found else None


_NAME = re.compile(r"^\s*([A-Za-z0-9][A-Za-z0-9._-]*)")
_EXTRA = re.compile(r"""extra\s*==\s*["']([^"']+)["']""")


def installed_extras() -> list[str]:
    """The extras of noeta whose every requirement is installed here."""
    dist = _distribution()
    if dist is None:
        return []
    needs: dict[str, list[str]] = {}
    for line in dist.requires or ():
        marker, package = _EXTRA.search(line), _NAME.match(line)
        if marker and package:
            needs.setdefault(marker.group(1), []).append(package.group(1))
    return sorted(extra for extra, names in needs.items() if all(map(_installed, names)))


def _installed(name: str) -> bool:
    try:
        importlib.metadata.distribution(name)
    except importlib.metadata.PackageNotFoundError:
        return False
    return True


def _requirement(name: str, extras: list[str]) -> str:
    return f"{name}[{','.join(extras)}]" if extras else name


def _with(entry: dict[str, Any]) -> str:
    """A receipt's requirement as ``--with`` takes it."""
    base = _requirement(str(entry["name"]), list(entry.get("extras") or []))
    if entry.get("git"):
        return f"{base} @ git+{entry['git']}"
    if entry.get("url"):
        return f"{base} @ {entry['url']}"
    if entry.get("path") or entry.get("directory"):
        return f"{base} @ file://{entry.get('path') or entry.get('directory')}"
    return base + str(entry.get("specifier") or "")


@dataclass
class Plan:
    """How this noeta is updated: ``kind`` (``uv``, ``pip`` or ``source``)
    and the command (shown only, for ``source``)."""

    kind: str
    command: list[str]
    note: str = ""


def uv_plan(receipt: Path) -> Plan:
    """The ``uv tool install --reinstall`` that keeps what ``receipt`` names."""
    tool = tomllib.loads(receipt.read_text("utf-8")).get("tool", {})
    requirements = [r for r in tool.get("requirements", []) if isinstance(r, dict)]
    mine = next((r for r in requirements if r.get("name") == "noeta"), {})
    cmd = [shutil.which("uv") or "uv", "tool", "install", "--reinstall",
           _requirement("noeta", list(mine.get("extras") or []))]
    for entry in requirements:
        if entry is not mine and entry.get("name"):
            cmd += ["--with", _with(entry)]
    if tool.get("python"):
        cmd += ["--python", str(tool["python"])]
    for index in tool.get("options", {}).get("index", []) or []:
        if isinstance(index, dict) and index.get("url"):
            cmd += ["--default-index" if index.get("default") else "--index", str(index["url"])]
    return Plan("uv", cmd)


def plan() -> Plan:
    """How the noeta running now is updated."""
    extras = installed_extras()
    source = direct_url()
    if source is not None:
        where = str(source.get("url") or "").removeprefix("file://")
        reinstall = ["uv", "tool", "install", "--reinstall", _requirement("noeta", extras)]
        return Plan("source", reinstall, note=where)
    receipt = Path(sys.prefix) / "uv-receipt.toml"
    if receipt.is_file():
        return uv_plan(receipt)
    requirement = _requirement("noeta", extras)
    if importlib.util.find_spec("pip") is None and shutil.which("uv"):
        return Plan("pip", [shutil.which("uv") or "uv", "pip", "install", "--python",
                            sys.executable, "-U", requirement])
    return Plan("pip", [sys.executable, "-m", "pip", "install", "-U", requirement])


def _shown(cmd: list[str]) -> str:
    return " ".join(shlex.quote(word) for word in cmd)


def _reported_version() -> str:
    """The version the noeta installed now reports (a new process: this one
    still runs the old code)."""
    try:
        out = subprocess.run([sys.executable, "-m", "noeta.cli.main", "--version"],
                             capture_output=True, text=True, timeout=60, check=False).stdout
    except (OSError, subprocess.SubprocessError):
        return ""
    return out.split(" ", 1)[0].strip()


def run(_args: argparse.Namespace) -> int:
    import httpx

    from noeta.host.stream_json import noeta_version

    installed = noeta_version()
    how = plan()
    if how.kind == "source":
        where = f" ({how.note})" if how.note else ""
        print(f"noeta update: this noeta ({installed}) runs from source{where}, so nothing is "
              "changed. To update the checkout: git pull, then uv sync --all-extras. To use the "
              f"release instead: {_shown(how.command)}")
        return 0
    try:
        latest = latest_version()
    except (httpx.HTTPError, ValueError) as exc:
        print("noeta update: cannot reach PyPI to check for a new version "
              f"({str(exc) or type(exc).__name__}); nothing changed", file=sys.stderr)
        return 1
    if not _newer(latest, installed):
        print(f"noeta is up to date ({installed})")
        return 0
    server = web_command.stop(quiet=True)
    if server is not None:
        print("noeta update: stopped noeta web; it starts again after the update")
    print(f"noeta update: {installed} -> {latest}\n$ {_shown(how.command)}", flush=True)
    try:
        code = subprocess.run(how.command, check=False).returncode
    except OSError as exc:
        print(f"noeta update: cannot run {how.command[0]}: {exc.strerror or exc}",
              file=sys.stderr)
        code = 1
    if server is not None:
        # The new noeta's code, in a new process.
        subprocess.run([sys.executable, "-m", "noeta.cli.main", "web", "--no-open",
                        "--port", str(server.port)], cwd=server.cwd or None, check=False)
    if code != 0:
        print(f"noeta update: the update failed (exit {code}); see the output above",
              file=sys.stderr)
        return code
    print(f"noeta update: now at {_reported_version() or latest}")
    return 0
