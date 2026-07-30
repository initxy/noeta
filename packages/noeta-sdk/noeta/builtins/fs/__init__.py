"""``fs`` — the default filesystem / shell tool pack.

The 9 fs tool classes; the refs point at the real tool classes so
``resolve()`` yields callable tool objects. NB: the *default* tool set of a
bare ``Options`` still comes from ``BUILTIN_TOOL_CLASSES`` in the compile path
(byte-identity), so activating ``fs`` is identity-inert — this declaration is
the reference manifest and the listing surface, not a second source of the
default tools.
"""

from __future__ import annotations

from noeta.builtins._declare import c
from noeta.client.plugin_manifest import PluginManifest


MANIFEST = PluginManifest(
    name="fs",
    requires_noeta=">=0.4",
    contributions=(
        c("tool", "read", "noeta.builtins.fs.impl.read:ReadFileTool"),
        c("tool", "glob", "noeta.builtins.fs.impl.read:GlobTool"),
        c("tool", "grep", "noeta.builtins.fs.impl.read:GrepTool"),
        c("tool", "edit", "noeta.builtins.fs.impl.edit:ReplaceTextTool"),
        c("tool", "write", "noeta.builtins.fs.impl.edit:WriteFileTool"),
        c("tool", "apply_patch", "noeta.builtins.fs.impl.patch:ApplyPatchTool"),
        c("tool", "shell_run", "noeta.builtins.fs.impl.shell:ShellRunTool"),
        c("tool", "shell_poll", "noeta.builtins.fs.impl.shell:ShellPollTool"),
        c("tool", "shell_kill", "noeta.builtins.fs.impl.shell:ShellKillTool"),
        # The session-construction half (microkernel phase 3): the factory
        # the kernel builder's generic pack loop calls. Band 100 — the base
        # pack leads the construction order.
        c(
            "session_pack",
            "fs",
            "noeta.builtins.fs.impl:build_fs_session_pack",
            priority=100,
        ),
    ),
)
