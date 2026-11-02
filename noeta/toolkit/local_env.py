"""What a command spawned on this machine inherits, and whether an exec env
is this machine."""

from __future__ import annotations

import os
import re
from collections.abc import Iterable, Mapping
from typing import Any

#: noeta's own credentials: variables only noeta reads (its provider key,
#: its store's DSN, its server and MCP client secrets, its search key). A
#: process noeta starts never gets them; everything else it inherits, as in
#: Claude Code, which likewise withholds only its own credential variables.
OWN_SECRETS = frozenset(
    {
        "NOETA_API_KEY",
        "NOETA_STORE",
        "NOETA_SERVER_TOKEN",
        "NOETA_MCP_CLIENT_SECRET",
        "NOETA_WEB_SEARCH_API_KEY",
    }
)


#: libpq's credential variables: withheld once noeta's own store is on
#: Postgres (they log in to it), passed otherwise.
POSTGRES_SECRETS = frozenset({"PGPASSWORD", "PGPASSFILE", "PGSERVICE", "PGSERVICEFILE"})

_ENV_REF = re.compile(r"\{env:([^{}]+)\}")
#: Further variables of noeta's own, learnt from its configuration.
_withheld: set[str] = set()


def withhold(names: Iterable[str]) -> None:
    """Keep ``names`` out of :func:`child_env` from now on (for this process)."""
    _withheld.update(names)


def withhold_env_refs(config: Any) -> None:
    """Withhold every variable an ``{env:NAME}`` reference in ``config``
    names, at any nesting: noeta's configuration hands those secrets to its
    own plugins and channels, so they are its credentials too. (A Claude
    Code plugin's own hooks and MCP servers name theirs; those pass.)"""
    found: set[str] = set()

    def walk(value: Any) -> None:
        if isinstance(value, str):
            found.update(_ENV_REF.findall(value))
        elif isinstance(value, Mapping):
            for item in value.values():
                walk(item)
        elif isinstance(value, (list, tuple)):
            for item in value:
                walk(item)

    walk(config)
    withhold(found)


def child_env() -> dict[str, str]:
    """The environment a process noeta starts on this machine (a command, a
    hook, a stdio MCP server) begins with: this process's, without
    :data:`OWN_SECRETS` and what :func:`withhold` named."""
    return {key: value for key, value in os.environ.items()
            if key not in OWN_SECRETS and key not in _withheld}


def is_local(env: object) -> bool:
    """Whether ``env`` runs commands on this machine (so facts about this
    machine, such as its shell and OS, describe it)."""
    return bool(getattr(env, "local", False))


async def env_platform(env: Any) -> str:
    """The platform ``env`` runs commands on, in ``sys.platform``'s words
    (``linux``, ``darwin``): this machine's for a local env, else from its
    ``uname -s``; ``""`` when it cannot be told."""
    import sys

    if is_local(env):
        return sys.platform
    try:
        out = await env.run(["uname", "-s"], timeout=5, output_cap=1024)
    except Exception:  # noqa: BLE001 — best-effort
        return ""
    if out.exit_code != 0 or out.timed_out:
        return ""
    name = out.stdout.decode("utf-8", errors="replace").strip().lower()
    return name if name in ("linux", "darwin") or name.startswith("freebsd") else ""


def local_timezone() -> str | None:
    """This machine's IANA time zone name (``Europe/Berlin``): ``TZ`` when it
    names one, else the zone ``/etc/localtime`` links to or ``/etc/timezone``
    names; ``None`` when it cannot be told. A front end on the person's
    machine passes it to the tasks it creates (task config ``timezone``)."""
    from zoneinfo import ZoneInfo

    candidates = [os.environ.get("TZ", "").lstrip(":")]
    target = os.path.realpath("/etc/localtime")
    if "zoneinfo/" in target:
        candidates.append(target.split("zoneinfo/", 1)[1])
    try:
        with open("/etc/timezone", encoding="utf-8") as f:
            candidates.append(f.read().strip())
    except OSError:
        pass
    for name in candidates:
        if not name or name.startswith(("/", ".")):
            continue
        try:
            ZoneInfo(name)
        except (ValueError, KeyError, OSError):
            continue
        return name
    return None
