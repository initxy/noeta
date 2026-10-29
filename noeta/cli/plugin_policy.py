"""The machine's say over which plugins load (``[policy]`` in the managed
``config.toml``)."""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class PluginPolicy:
    """The machine's say over plugins (``/etc/noeta/config.toml``
    ``[policy]``): ``allowed_plugins`` (when set, only these plugins that
    noeta does not ship load) and ``blocked_plugins`` (never load). It wins
    over the user's and the project's settings; a plugin file is checked by
    its file name before it is imported, every plugin, channel and Claude
    Code plugin by its name."""

    allowed: frozenset[str] | None = None
    blocked: frozenset[str] = frozenset()

    def admits(self, name: str, *, shipped: bool = False) -> bool:
        if name in self.blocked:
            return False
        return shipped or self.allowed is None or name in self.allowed


def plugin_policy(managed: Path) -> PluginPolicy:
    """The policy in ``<managed>/config.toml`` (the machine's directory,
    ``noeta.cli.settings_files.managed_dir``); none when it has none."""
    path = managed / "config.toml"
    if not path.is_file():
        return PluginPolicy()
    table = tomllib.loads(path.read_text("utf-8")).get("policy") or {}
    allowed = table.get("allowed_plugins")
    return PluginPolicy(allowed=None if allowed is None else frozenset(map(str, allowed)),
                        blocked=frozenset(map(str, table.get("blocked_plugins") or ())))
