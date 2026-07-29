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
        c("tool", "read", "noeta.tools.fs.read:ReadFileTool"),
        c("tool", "glob", "noeta.tools.fs.read:GlobTool"),
        c("tool", "grep", "noeta.tools.fs.read:GrepTool"),
        c("tool", "edit", "noeta.tools.fs.edit:ReplaceTextTool"),
        c("tool", "write", "noeta.tools.fs.edit:WriteFileTool"),
        c("tool", "apply_patch", "noeta.tools.fs.patch:ApplyPatchTool"),
        c("tool", "shell_run", "noeta.tools.fs.shell:ShellRunTool"),
        c("tool", "shell_poll", "noeta.tools.fs.shell:ShellPollTool"),
        c("tool", "shell_kill", "noeta.tools.fs.shell:ShellKillTool"),
    ),
)
