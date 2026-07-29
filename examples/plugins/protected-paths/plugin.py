"""First-party Noeta plugin — ``protected-paths``: a path-containment Guard.

Demonstrated SDK capability
---------------------------
A **manifest plugin** (the SDK-extensibility redesign,
``docs/implementation-specs/2026-07-28-sdk-extensibility-redesign.md``, D1) that
contributes a single :class:`~noeta.sdk.Guard` on the ``guard`` surface. The
guard is *governance* authority: once the plugin is loaded it is in force for
**every** agent in the process, regardless of which agents activate which
plugins (spec D6) — an operator must not be able to opt an agent out of a write
fence by omitting an activation.

What the guard does
-------------------
It inspects every **file-mutating** built-in fs tool call and DENIES it when the
target path escapes a configured allowlist of roots, or matches an optional
deny-glob. It is the packaged, operator-configurable form of the ad-hoc
``can_use_tool`` path check: a plugin any host can enable **by name** without
writing guard code.

The mutating built-in fs tools it inspects, and the path arguments it reads
(the real tool schemas live under
``packages/noeta-runtime/noeta/tools/fs/``):

* ``edit``        — ``arguments["path"]``
* ``write``       — ``arguments["path"]``
* ``apply_patch`` — ``arguments["edits"][*]["path"]`` (every edit in the batch)

Every other tool (``read`` / ``glob`` / ``grep`` / ``shell_run`` / any custom
tool) and every non-tool action (spawn / finish) is allowed untouched — the
guard is about *where writes land*, nothing else. ``shell_run`` is deliberately
out of scope: a shell can touch anything, so a path guard cannot fence it.
Confine shell IO with a sandbox execution environment instead.

Containment is LEXICAL
----------------------
A candidate path is normalized with ``os.path.normpath`` (so ``..`` segments
collapse) and, when relative, joined against each allowed root before the
component-wise containment test (:func:`noeta.sdk.path_within`). This catches
the two classic escapes:

* ``../../etc/passwd`` — the ``..`` run collapses to a path outside every root.
* ``/etc/passwd`` (absolute) — normalizes to itself, contained by no root.

It does **NOT resolve symlinks**. A symlink that lives inside an allowed root
but points outside it will pass this guard. Lexical containment is a guardrail
against accidental or obvious escapes, not a security sandbox; for symlink-safe
fencing use the runtime's realpath-based ``WorkspaceRoot`` or a real sandbox
execution environment. This trade-off is deliberate and is restated in the
README.

Configuration (environment, not per-plugin config dict)
-------------------------------------------------------
The manifest mechanism resolves a contribution's ``ref`` to a live object; it
does **not** thread a per-plugin config dict (that would make agent identity
depend on operator config). Configuration is therefore **orthogonal**, read
from the environment when the module is imported:

* ``NOETA_PROTECTED_PATHS_ROOTS`` — ``os.pathsep``-separated writable roots.
  Absent ⇒ the process working directory (so the guard always protects
  *something* — a fence that protects nothing is a bug, not a default).
* ``NOETA_PROTECTED_PATHS_DENY_GLOBS`` — comma-separated ``fnmatch`` patterns;
  a match on the raw path, its normalized absolute form, or its basename DENIES
  the call even inside an allowed root. Denies always win over containment.

A host injects these before it loads the plugin (see the reference host, which
sets ``NOETA_PROTECTED_PATHS_ROOTS`` to the session workspace). The
:class:`ProtectedPathsGuard` is independently constructable and unit-testable —
the manifest only packages it.
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


#: The file-mutating built-in fs tools this guard inspects. Read-only tools
#: (``read`` / ``glob`` / ``grep``) and ``shell_run`` are intentionally out of
#: scope (a shell escapes any path fence — use a sandbox exec env for that).
MUTATING_FS_TOOLS: frozenset[str] = frozenset({"edit", "write", "apply_patch"})


def _iter_target_paths(
    tool_name: str, arguments: Mapping[str, Any]
) -> Iterator[str]:
    """Yield each non-empty string target path a mutating call would write.

    Non-string / missing path fields are skipped (not denied): schema
    validation is the tool's job, and a malformed call writes nothing anyway.
    The guard only rules on paths it can actually read.
    """
    if tool_name in ("edit", "write"):
        p = arguments.get("path")
        if isinstance(p, str) and p:
            yield p
    elif tool_name == "apply_patch":
        edits = arguments.get("edits")
        if isinstance(edits, list):
            for edit in edits:
                if isinstance(edit, Mapping):
                    p = edit.get("path")
                    if isinstance(p, str) and p:
                        yield p


def _lexical_abspath(raw: str, base: Path) -> Path:
    """Absolute, ``..``-collapsed form of ``raw`` (no symlink resolution).

    Absolute inputs normalize to themselves; relative inputs are joined onto
    ``base`` first. ``os.path.normpath`` collapses ``..`` textually — it never
    touches the filesystem, so symlinks are NOT followed (documented caveat).
    """
    if os.path.isabs(raw):
        return Path(os.path.normpath(raw))
    return Path(os.path.normpath(os.path.join(str(base), raw)))


class ProtectedPathsGuard:
    """Deny a mutating fs tool call whose target path escapes the allowlist.

    Priority 15 places it just ahead of the built-in ``PermissionGuard`` (20):
    a path escape is a hard boundary, decided before the coarser allow/deny-list
    logic. Duplicate priorities are fine (the ``HookManager`` keeps a stable
    order); the number only fixes *when* the check runs, never *whether*.

    Constructable and unit-testable on its own; the manifest packages a
    configured instance built from the environment (see the module docstring).
    """

    name = "protected_paths"
    priority = 15

    def __init__(
        self,
        allowed_roots: Sequence[Any] = (),
        deny_globs: Sequence[str] = (),
    ) -> None:
        # Roots are canonicalised once, lexically (cwd-relative + ``..``
        # collapsed, no realpath) — the same normalization the per-call check
        # applies to candidate paths, so both sides speak one form.
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
            return True  # containment disabled → deny-glob-only mode
        return any(
            path_within(_lexical_abspath(raw, root), root)
            for root in self._allowed_roots
        )

    def _deny_glob_hit(self, raw: str) -> Optional[str]:
        if not self._deny_globs:
            return None
        base = self._allowed_roots[0] if self._allowed_roots else Path.cwd()
        resolved = _lexical_abspath(raw, base)
        # Match a glob against the raw path, its normalized absolute form, and
        # its basename — a denylist errs broad on purpose.
        candidates = (raw, resolved.as_posix(), Path(raw).name)
        for glob in self._deny_globs:
            if any(fnmatch.fnmatch(candidate, glob) for candidate in candidates):
                return glob
        return None


# ---------------------------------------------------------------------------
# Environment-sourced configuration (see the module docstring).
# ---------------------------------------------------------------------------


def _roots_from_env() -> tuple[str, ...]:
    raw = os.environ.get("NOETA_PROTECTED_PATHS_ROOTS")
    if raw:
        parts = tuple(p for p in raw.split(os.pathsep) if p.strip())
        if parts:
            return parts
    return (os.getcwd(),)  # never empty — a fence that protects nothing is a bug


def _globs_from_env() -> tuple[str, ...]:
    raw = os.environ.get("NOETA_PROTECTED_PATHS_DENY_GLOBS")
    if not raw:
        return ()
    return tuple(g.strip() for g in raw.split(",") if g.strip())


#: The declarative config schema, carried on the manifest for operator tooling.
#: Descriptive only — the mechanism never reads it (config is environment-sourced).
CONFIG_SCHEMA = {
    "env": {
        "NOETA_PROTECTED_PATHS_ROOTS": "os.pathsep-separated writable roots (default: cwd)",
        "NOETA_PROTECTED_PATHS_DENY_GLOBS": "comma-separated fnmatch deny globs (default: none)",
    }
}


#: The configured guard the manifest ships. Built once from the environment at
#: import; a distributed install exposes it at the ``ref`` below
#: (``protected_paths:GUARD``), while the single-file load caches this very
#: object so resolution never re-imports.
GUARD = ProtectedPathsGuard(
    allowed_roots=_roots_from_env(), deny_globs=_globs_from_env()
)


#: The single-file manifest (decorator sugar *is* the manifest, spec D1). The
#: builder name is the plugin identity — the enable-list key and the collision
#: label. ``python -m noeta.sdk.plugin_check`` derives the TOML from this builder
#: and verifies it against the shipped ``noeta-plugin.toml`` / ``[tool.noeta]``.
plugin = PluginBuilder(
    "protected-paths", requires_noeta=">=0.4", config_schema=CONFIG_SCHEMA
)
plugin.contribute("guard", GUARD, name="protected_paths", ref="protected_paths:GUARD")
