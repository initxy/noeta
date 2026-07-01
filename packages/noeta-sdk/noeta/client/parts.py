"""SDK built-in parts table.

This module is the **single source** for the built-in tool-name → class
mapping and the policy/composer ComponentRefs (roster removed; there is no
``noeta.agent.roster.specs`` mirror). The SDK owns these identity constants;
the product side consumes the compiled output via :mod:`noeta.client.options`
+ :mod:`noeta.presets`, with no second copy.

Keeping the table in ``noeta.client`` (rather than importing from ``noeta.agent``)
makes ``noeta-sdk`` self-contained so library users do not pull in the coding-agent
product package.
"""

from __future__ import annotations

from dataclasses import MISSING, fields
from typing import Any

from noeta.agent.spec import ComponentRef, ToolRef
from noeta.tools.fs.edit import ReplaceTextTool, WriteFileTool
from noeta.tools.fs.patch import ApplyPatchTool
from noeta.tools.fs.read import GlobTool, GrepTool, ReadFileTool
from noeta.tools.fs.shell import ShellKillTool, ShellPollTool, ShellRunTool
from noeta.tools.web.fetch import WebFetchTool
from noeta.tools.web.search import WebSearchTool


__all__ = [
    "BUILTIN_TOOL_CLASSES",
    "COMPOSER_REF",
    "POLICY_REF",
    "builtin_tool_ref",
]


#: name → fs tool class. SDK single source
#: (``roster.specs._FS_TOOL_CLASSES`` removed; no mirror copy).
_FS_TOOL_CLASSES: dict[str, type] = {
    "read": ReadFileTool,
    "glob": GlobTool,
    "grep": GrepTool,
    "edit": ReplaceTextTool,
    "write": WriteFileTool,
    "apply_patch": ApplyPatchTool,
    "shell_run": ShellRunTool,
    # shell_poll / shell_kill join the built-in catalog so
    # they can enter a preset whitelist (main / general-purpose via tools=None).
    # The background mechanism itself is unchanged; this only makes the
    # already-existing tool classes addressable by name through builtin_tool_ref.
    "shell_poll": ShellPollTool,
    "shell_kill": ShellKillTool,
}


#: name → web tool class. Phase 2: ``webfetch`` is a built-in but
#: NOT an fs tool (it takes an HTTP transport, no ``WorkspaceRoot``), so it
#: lives in its own group rather than ``_FS_TOOL_CLASSES``. Joining
#: ``BUILTIN_TOOL_CLASSES`` makes it addressable by name in a preset whitelist
#: and puts it in ``main``'s ``tools=None`` full-catalog set; explore / plan /
#: general-purpose keep their explicit whitelists (no webfetch).
#:
#: ``web_search`` is whitelisted the same way (so ``main`` may receive it), but
#: it is constructed only when ``NOETA_WEB_SEARCH_API_KEY`` is set: with no key
#: ``build_web_tools`` omits it from the runtime pack, so the whitelist's
#: intersection with the pack drops it — the model never sees a search tool it
#: cannot use. Listing it here costs nothing without a key.
_WEB_TOOL_CLASSES: dict[str, type] = {
    "webfetch": WebFetchTool,
    "web_search": WebSearchTool,
}


#: Alias to the built-in tool classes — the single mapping
#: :func:`builtin_tool_ref` consults. Missing names raise ``KeyError`` loudly
#: so we never mint a tool ref with guessed metadata.
BUILTIN_TOOL_CLASSES: dict[str, type] = {
    **_FS_TOOL_CLASSES,
    **_WEB_TOOL_CLASSES,
}


#: ReAct decision-mapping behaviour version — SDK single source
#: (``roster.specs._REACT_POLICY`` removed).
POLICY_REF = ComponentRef("react", "1")
#: Three-segment context composer version — SDK single source
#: (``roster.specs._THREE_SEGMENT_COMPOSER`` removed).
COMPOSER_REF = ComponentRef("three_segment", "v3")


def _field_default(cls: type, field_name: str) -> Any:
    """Return the static dataclass-field default of ``cls.field_name``.

    SDK single implementation (``roster.specs._field_default`` removed).
    Raises ``TypeError`` if the field has no static default (callers would
    otherwise silently get a ``MISSING`` sentinel into the AgentSpec identity,
    which is the bug this guard prevents).
    """
    for f in fields(cls):
        if f.name == field_name:
            if f.default is MISSING:
                raise TypeError(
                    f"{cls.__name__}.{field_name} has no static default; "
                    f"cannot read tool identity metadata without instantiation"
                )
            return f.default
    raise AttributeError(f"{cls.__name__} has no field {field_name!r}")


def builtin_tool_ref(name: str) -> ToolRef:
    """Return a :class:`ToolRef` for the built-in tool ``name``.

    ``version`` is hard-coded to ``"1"`` (the SDK-wide convention for
    built-in tools; a bump in tool behaviour should add a new name or
    bump the component refs). ``risk_level`` is read straight off the tool
    class' static default, so a class-level change in risk surfaces in the
    compiled ``AgentSpec``.

    Raises
    ------
    KeyError
        If ``name`` is not in :data:`BUILTIN_TOOL_CLASSES`. The message
        enumerates the valid names.
    """
    if name not in BUILTIN_TOOL_CLASSES:
        available = ", ".join(sorted(BUILTIN_TOOL_CLASSES))
        raise KeyError(
            f"Unknown built-in tool {name!r}. Available: {available}"
        )
    cls = BUILTIN_TOOL_CLASSES[name]
    return ToolRef(
        name=name,
        version="1",
        risk_level=str(_field_default(cls, "risk_level")),
    )
