"""``fs`` — the default filesystem / shell tool pack, declared as a manifest.

Each ``tool`` ref points at the concrete tool class so ``resolve()`` yields
callable tool objects. A bare ``Options`` draws its default tools from
``builtin_tool_classes()``, so activating ``fs`` is identity-inert: this
declaration is the reference manifest and the listing surface, not a second
source of the default tools.
"""

from __future__ import annotations

from noeta.builtins._declare import c
from noeta.client.plugin_manifest import PluginManifest


MANIFEST = PluginManifest(
    name="fs",
    requires_noeta=">=0.4",
    contributions=(
        c("tool", "Read", "noeta.builtins.fs.impl.read:ReadFileTool"),
        c("tool", "Glob", "noeta.builtins.fs.impl.read:GlobTool"),
        c("tool", "Grep", "noeta.builtins.fs.impl.read:GrepTool"),
        c("tool", "Edit", "noeta.builtins.fs.impl.edit:ReplaceTextTool"),
        c("tool", "Write", "noeta.builtins.fs.impl.edit:WriteFileTool"),
        c("tool", "Bash", "noeta.builtins.fs.impl.shell:ShellRunTool"),
        c("tool", "BashOutput", "noeta.builtins.fs.impl.shell:ShellPollTool"),
        c("tool", "KillShell", "noeta.builtins.fs.impl.shell:ShellKillTool"),
        # Band 100 sorts first, so this base pack is constructed before any
        # capability pack that appends to it.
        c(
            "session_pack",
            "fs",
            "noeta.builtins.fs.impl:build_fs_session_pack",
            priority=100,
        ),
    ),
)
