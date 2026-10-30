"""What a command spawned on this machine inherits, and whether an exec env
is this machine."""

from __future__ import annotations

import os
from collections.abc import Iterable

#: The only parent-environment variables a spawned command inherits, so a
#: child never picks up the host's credentials by accident. A host that needs
#: more names them in ``LocalExecEnv(inherit_env=...)``.
ENV_ALLOWLIST = frozenset(
    {
        "PATH",
        "HOME",
        "LANG",
        "LC_ALL",
        "TERM",
        "TMPDIR",
        # Python + uv interpreter discovery
        "PYTHONHASHSEED",
        "PYTHONPATH",
        "VIRTUAL_ENV",
    }
)


def scrub_env(
    allowlist: Iterable[str] | None = None,
    *,
    inherit: Iterable[str] = (),
) -> dict[str, str]:
    """A minimal environment built from the parent's: :data:`ENV_ALLOWLIST`
    (or ``allowlist``) plus the ``inherit`` names, nothing else."""
    parent = os.environ
    keys = set(ENV_ALLOWLIST if allowlist is None else allowlist) | set(inherit)
    return {key: parent[key] for key in sorted(keys) if key in parent}


def is_local(env: object) -> bool:
    """Whether ``env`` runs commands on this machine (so facts about this
    machine, such as its shell and OS, describe it)."""
    return bool(getattr(env, "local", False))


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
