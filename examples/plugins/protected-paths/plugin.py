"""A write fence: deny any file-mutating tool call whose target escapes a root.

Demonstrated SDK capability: the ``guard`` surface. A guard is process-scoped
governance — once the plugin is loaded the fence applies to every agent, so an
operator cannot opt one agent out by leaving the plugin off its activation list.
This is the packaged form of an ad-hoc ``can_use_tool`` path check: a host
enables it by name instead of writing guard code.

Containment is **lexical**: paths are ``normpath``-collapsed and tested
component-wise, never resolved. A symlink inside an allowed root that points
outside it passes this guard. That is a deliberate trade-off — a textual check
cannot touch the filesystem and so cannot be raced or made to block — but it
means this is a guardrail against accidental escapes, **not** a security
sandbox. For symlink-safe fencing use the runtime's realpath-based
``WorkspaceRoot``; for isolation use a sandbox execution environment.

``shell_run`` is out of scope for the same reason: a shell can reach anything, so
a path fence around it would be theatre.

The manifest mechanism resolves a contribution ``ref`` to a live object and
threads no per-plugin config dict — otherwise operator configuration would leak
into agent identity — so the shipped :data:`GUARD` reads its roots and globs from
the environment at import.
"""

from __future__ import annotations

import fnmatch
import os
from collections.abc import Iterator, Mapping, Sequence
from pathlib import Path
from typing import Any, Optional

from noeta.sdk import (
    GuardContext,
    PluginBuilder,
    ProposedAction,
    ProposedToolCall,
    VerdictResult,
    path_within,
)


#: Only the file-mutating built-in fs tools. Read-only tools are out of scope
#: because this fence is about where writes land, and ``shell_run`` is out of
#: scope because a shell escapes any path fence.
MUTATING_FS_TOOLS: frozenset[str] = frozenset({"Edit", "Write"})


def _iter_target_paths(
    tool_name: str, arguments: Mapping[str, Any]
) -> Iterator[str]:
    """Yield each non-empty string target path a mutating call would write.

    Malformed path fields are skipped rather than denied: schema validation is
    the tool's job, a malformed call writes nothing anyway, and a guard that
    ruled on arguments it cannot read would deny on tool-schema changes it has
    no opinion about.
    """
    if tool_name in ("Edit", "Write"):
        p = arguments.get("file_path")
        if isinstance(p, str) and p:
            yield p


def _lexical_abspath(raw: str, base: Path) -> Path:
    """Absolute, ``..``-collapsed form of ``raw`` — no symlink resolution.

    ``normpath`` is purely textual, which is what catches ``../../etc/passwd``
    without touching the filesystem, and equally what lets a symlink through.
    """
    if os.path.isabs(raw):
        return Path(os.path.normpath(raw))
    return Path(os.path.normpath(os.path.join(str(base), raw)))


class ProtectedPathsGuard:
    """Deny a mutating fs tool call whose target path escapes the allowlist.

    Priority 15 runs ahead of the built-in ``PermissionGuard`` (20): a path
    escape is a hard boundary and should be the reason reported, not whichever
    coarser allow/deny-list rule happens to fire first. The number fixes *when*
    the check runs, never *whether* — duplicate priorities are fine, the
    ``HookManager`` keeps a stable order.
    """

    name = "protected_paths"
    priority = 15

    def __init__(
        self,
        allowed_roots: Sequence[Any] = (),
        deny_globs: Sequence[str] = (),
    ) -> None:
        # Roots go through the same lexical normalization the per-call check
        # applies to candidates, so both sides of the containment test speak one
        # form. Mixing realpath'd roots with textual candidates would silently
        # fail to contain anything under a symlinked workspace.
        self._allowed_roots: tuple[Path, ...] = tuple(
            _lexical_abspath(str(r), Path.cwd()) for r in allowed_roots
        )
        self._deny_globs: tuple[str, ...] = tuple(str(g) for g in deny_globs)

    def check(
        self, action: ProposedAction, ctx: GuardContext
    ) -> VerdictResult:
        if not isinstance(action, ProposedToolCall):
            return VerdictResult.allow()
        call = action.call
        if call.tool_name not in MUTATING_FS_TOOLS:
            return VerdictResult.allow()
        for raw in _iter_target_paths(call.tool_name, call.arguments):
            hit = self._deny_glob_hit(raw)
            if hit is not None:
                return VerdictResult.deny(
                    f"protected-paths: {raw!r} matches deny glob {hit!r}"
                )
            if not self._within_allowed(raw):
                return VerdictResult.deny(
                    f"protected-paths: {raw!r} (tool {call.tool_name!r}) "
                    f"escapes the allowed roots "
                    f"{[str(r) for r in self._allowed_roots]}"
                )
        return VerdictResult.allow()

    def _within_allowed(self, raw: str) -> bool:
        if not self._allowed_roots:
            # No roots means deny-glob-only, not deny-everything: a host that
            # configures globs alone should get exactly what it asked for.
            return True
        return any(
            path_within(_lexical_abspath(raw, root), root)
            for root in self._allowed_roots
        )

    def _deny_glob_hit(self, raw: str) -> Optional[str]:
        if not self._deny_globs:
            return None
        base = self._allowed_roots[0] if self._allowed_roots else Path.cwd()
        resolved = _lexical_abspath(raw, base)
        # A denylist errs broad on purpose: matching the raw path, its absolute
        # form and its basename means ``*.pem`` fences the file however the
        # model chose to spell the path.
        candidates = (raw, resolved.as_posix(), Path(raw).name)
        for glob in self._deny_globs:
            if any(fnmatch.fnmatch(candidate, glob) for candidate in candidates):
                return glob
        return None


def _roots_from_env() -> tuple[str, ...]:
    raw = os.environ.get("NOETA_PROTECTED_PATHS_ROOTS")
    if raw:
        parts = tuple(p for p in raw.split(os.pathsep) if p.strip())
        if parts:
            return parts
    # Never empty. An unset variable must not silently turn the fence off — a
    # guard that protects nothing is worse than no guard, because it reads as one.
    return (os.getcwd(),)


def _globs_from_env() -> tuple[str, ...]:
    raw = os.environ.get("NOETA_PROTECTED_PATHS_DENY_GLOBS")
    if not raw:
        return ()
    return tuple(g.strip() for g in raw.split(",") if g.strip())


#: Carried on the manifest so operator tooling can list the knobs. Descriptive
#: only — nothing in the loader reads it, so it must be kept true by hand.
CONFIG_SCHEMA = {
    "env": {
        "NOETA_PROTECTED_PATHS_ROOTS": "os.pathsep-separated writable roots (default: cwd)",
        "NOETA_PROTECTED_PATHS_DENY_GLOBS": "comma-separated fnmatch deny globs (default: none)",
    }
}


#: The configured guard the manifest ships. Built once at import, so a host must
#: set the roots *before* loading the plugin. A distributed install resolves it
#: through the ``ref`` below; a single-file load caches this very object, so the
#: two paths agree without a second import.
GUARD = ProtectedPathsGuard(
    allowed_roots=_roots_from_env(), deny_globs=_globs_from_env()
)


#: The builder *is* this plugin's manifest, and its name is the plugin identity
#: — the enable-list key and collision label, not the filename.
#: ``python -m noeta.sdk.plugin_check`` derives TOML from it and verifies the
#: shipped ``noeta-plugin.toml`` matches, which is what stops the two drifting.
plugin = PluginBuilder(
    "protected-paths", requires_noeta=">=0.4", config_schema=CONFIG_SCHEMA
)
plugin.contribute("guard", GUARD, name="protected_paths", ref="protected_paths:GUARD")
