"""First-party Noeta plugin — ``protected-paths``: a path-containment Guard.

What it does
------------
This plugin contributes a single :class:`~noeta.protocols.hooks.Guard` that
inspects every **file-mutating** built-in fs tool call and DENIES it when the
target path escapes a configured allowlist of roots, or matches an optional
deny-glob. It is the packaged, operator-configurable form of the ad-hoc
``can_use_tool`` path check: a plugin any host can enable **by name** without
writing guard code (see ``docs/adr/plugin-contribution-bundles.md``).

The mutating built-in fs tools it inspects, and the path arguments it reads
(the real tool schemas live under
``packages/noeta-runtime/noeta/tools/fs/``):

* ``edit``        — ``arguments["path"]``
* ``write``       — ``arguments["path"]``
* ``apply_patch`` — ``arguments["edits"][*]["path"]`` (every edit in the batch)

Every other tool (``read`` / ``glob`` / ``grep`` / ``shell_run`` / any custom
tool) and every non-tool action (spawn / finish) is allowed untouched — the
guard is about *where writes land*, nothing else. ``shell_run`` is
deliberately out of scope: a shell can touch anything, so a path guard cannot
fence it. Confine shell IO with a sandbox execution environment instead.

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

Configuration
-------------
The factory reads two keys from its config dict (both individually optional)::

    {
        "allowed_roots": ["/abs/workspace", "/abs/scratch"],
        "deny_globs": ["*.env", "*/secrets/*", "id_rsa"]
    }

* ``allowed_roots`` — directories a write may land in. Relative entries are
  resolved against the process cwd. Empty ⇒ the containment check is disabled
  (deny-glob-only mode).
* ``deny_globs`` — ``fnmatch`` patterns; a match on the raw path, its
  normalized absolute form, or its basename DENIES the call even inside an
  allowed root. Denies always win over containment.

Enabling the plugin with neither key is a misconfiguration and fails the client
build loudly (a protection guard that protects nothing is a bug, not a default).

Wiring
------
The guard folds into ``Options.guards`` via :func:`noeta.sdk.merge_plugins`;
the operator passes per-plugin config through
``load_plugins(config={"protected-paths": {...}})``. See the README for the
entry-point and explicit-path load recipes.
"""

from __future__ import annotations

import fnmatch
import os
from collections.abc import Iterator, Mapping, Sequence
from pathlib import Path
from typing import Any, Optional

from noeta.sdk import (
    GuardContext,
    PluginError,
    ProposedAction,
    ProposedToolCall,
    VerdictResult,
    path_within,
)


#: The loader derives a plugin's name from its module/file stem; this file is
#: ``plugin.py``, so we override the derived name to the intended identity. The
#: name is the enable-list key, the ``config`` key, and the collision label.
noeta_plugin_name = "protected-paths"


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

    Constructable and unit-testable on its own; the :func:`noeta_plugin` factory
    is the packaged path that builds it from operator config.
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


def _as_str_sequence(value: Any, key: str) -> tuple[str, ...]:
    """Coerce a config value to a tuple of non-empty strings, or raise loudly.

    A bare string is rejected explicitly: ``"allowed_roots": "/ws"`` is almost
    always a mistake for ``["/ws"]``, and silently iterating its characters
    would be a nasty surprise.
    """
    if isinstance(value, str):
        raise PluginError(
            f"protected-paths: {key!r} must be a list of strings, not a bare "
            f"string ({value!r})"
        )
    if not isinstance(value, Sequence):
        raise PluginError(
            f"protected-paths: {key!r} must be a list of strings; got "
            f"{type(value).__name__}"
        )
    out: list[str] = []
    for item in value:
        if not isinstance(item, str) or not item.strip():
            raise PluginError(
                f"protected-paths: every {key!r} entry must be a non-empty "
                f"string; got {item!r}"
            )
        out.append(item)
    return tuple(out)


def noeta_plugin(api: Any, config: Optional[Mapping[str, Any]] = None) -> None:
    """Contribute the :class:`ProtectedPathsGuard`, configured from ``config``.

    ``config`` is the per-plugin dict the loader threads through
    ``load_plugins(config={"protected-paths": {...}})`` (it is ``{}`` when the
    operator enables the plugin without config). Both keys are individually
    optional, but enabling the plugin with NEITHER an allowed root nor a deny
    glob is a misconfiguration and raises :class:`~noeta.sdk.PluginError` — a
    guard that protects nothing would silently pass every write.
    """
    cfg = dict(config or {})
    allowed_roots = _as_str_sequence(cfg.get("allowed_roots", ()), "allowed_roots")
    deny_globs = _as_str_sequence(cfg.get("deny_globs", ()), "deny_globs")
    if not allowed_roots and not deny_globs:
        raise PluginError(
            "protected-paths: configure at least one 'allowed_roots' entry or "
            "one 'deny_globs' entry — a guard with neither protects nothing"
        )
    api.add_guard(
        ProtectedPathsGuard(allowed_roots=allowed_roots, deny_globs=deny_globs)
    )
