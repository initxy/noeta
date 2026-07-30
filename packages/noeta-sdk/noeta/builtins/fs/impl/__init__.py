"""``noeta.builtins.fs.impl`` — the file-system tool pack implementation.

The ``fs`` built-in plugin's body (microkernel migration, M1): the tool
classes plus the closure-construction factory. The pack is
*closure-constructed*: :func:`build_fs_tools` takes one ``WorkspaceRoot``
(the path-containment seam), an ``FsWriteMode`` (the ``DRY_RUN`` / ``APPLY``
policy for the edit tools), and a ``ShellMode`` (the ``OFF`` / ``ALLOWLIST``
/ ``ARBITRARY`` policy for the shell tools), and returns the dict of Tool
instances keyed by their provider-safe ``snake_case`` name. Each tool keeps a
reference to the workspace + its mode so the runtime never has to pass them
in (the L0 ``Tool`` Protocol stays unchanged).

The policy types themselves (``WorkspaceRoot`` / ``FsWriteMode`` /
``ShellMode`` / the shell allowlist / ``ExecEnv``) are **kernel**
infrastructure in ``noeta.runtime`` — the kernel and other consumers depend
on them without touching this plugin body. This module is reached only
through the plugin loader's ``ref`` resolution (or by the pack factory ref);
nothing imports it statically.
"""

from __future__ import annotations

from typing import Any, Mapping, Optional, Sequence, cast

from noeta.builtins.fs.impl.edit import (
    WRITE_FILE_MAX_BYTES,
    ReplaceTextTool,
    WriteFileTool,
)
from noeta.builtins.fs.impl.patch import (
    MAX_PATCH_CANONICAL_BYTES,
    MAX_PATCH_EDITS,
    ApplyPatchTool,
)
from noeta.builtins.fs.impl.read import GlobTool, GrepTool, ReadFileTool
from noeta.builtins.fs.impl.shell import (
    ShellKillTool,
    ShellPollTool,
    ShellRunTool,
)
from noeta.execution.session_pack import PackContribution, SessionBuildContext
from noeta.protocols.tool import Tool
from noeta.runtime.exec_env import ExecEnv, LocalExecEnv
from noeta.builtins.fs.impl.shell_rules import DEFAULT_SHELL_RULES
from noeta.runtime.shell_policy import ShellMode, build_allowlist
from noeta.runtime.workspace import (
    FsWriteMode,
    WorkspaceRoot,
    WriteRootsResolver,
)


__all__ = [
    "ApplyPatchTool",
    "FsToolPack",
    "GlobTool",
    "GrepTool",
    "MAX_PATCH_CANONICAL_BYTES",
    "MAX_PATCH_EDITS",
    "ReadFileTool",
    "ReplaceTextTool",
    "ShellKillTool",
    "ShellPollTool",
    "ShellRunTool",
    "WRITE_FILE_MAX_BYTES",
    "WriteFileTool",
    "build_fs_session_pack",
    "build_fs_tools",
]


def build_fs_tools(
    workspace: WorkspaceRoot,
    *,
    mode: FsWriteMode = FsWriteMode.DRY_RUN,
    shell_mode: ShellMode = ShellMode.ALLOWLIST,
    shell_allowlist: Sequence[Mapping[str, Any]] = (),
    write_path_globs: tuple[str, ...] = (),
    write_roots: Optional[WriteRootsResolver] = None,
    exec_env: Optional[ExecEnv] = None,
) -> dict[str, Tool]:
    """Build the fs tool pack sharing one ``WorkspaceRoot`` + write/shell modes.

    Defaults are the safe closures: ``DRY_RUN`` writes (a daemon that
    forgets ``--allow-write`` emits diff artifacts but does not write)
    and ``ALLOWLIST`` shell (a daemon that forgets ``--allow-shell``
    refuses arbitrary commands). The git convenience tools are always
    present — they are narrow read-only operations and are useful even
    when ``shell_run`` is fully ``OFF``.

    ``write_path_globs`` injects a workspace-relative path
    whitelist into the ``write`` tool — empty ⇒ unrestricted (default,
    identical to pre-whitelist builds); non-empty ⇒ ``write`` refuses any path outside the globs
    (e.g. passing ``("plans/*.md",)`` physically confines a writer to that
    directory). It only affects ``write``; ``edit`` / ``apply_patch`` ignore
    the whitelist.

    ``write_roots`` is the host's authorization seam for writes OUTSIDE the
    workspace: ``task_id -> the directories this task may also write``,
    consulted per call by ``edit`` / ``write`` / ``apply_patch``. ``None``
    (default) keeps the single-root wall — a write that resolves outside the
    workspace fails, full stop. A host that can *ask someone* (the noeta-agent
    product pauses the call for the owner's ruling and remembers the answer as
    a durable grant) passes a resolver here so the approved directory is open
    on the resumed call. Reads are never fenced and ignore this entirely.

    ``exec_env`` is the execution backend the fs / shell tools route their real
    IO through. ``None`` (default) ⇒ each tool builds its own ``LocalExecEnv``
    (byte-identical to the pre-seam host behaviour); a sandbox backend
    (:class:`~noeta.runtime.exec_env.AioSandboxExecEnv`) makes the whole pack
    act against a container — paired with a container ``workspace``
    (:meth:`WorkspaceRoot.for_container`). It never changes a tool's
    name / schema / description, so the stable prefix is unaffected.
    """
    # One backend shared across the pack — ``LocalExecEnv`` is stateless and
    # documented as reuse-safe, so the default path is byte-identical to each
    # tool's own ``default_factory=LocalExecEnv``.
    env: ExecEnv = LocalExecEnv() if exec_env is None else exec_env
    tools: list[Tool] = [
        ReadFileTool(workspace=workspace, exec_env=env),
        GlobTool(workspace=workspace, exec_env=env),
        GrepTool(workspace=workspace, exec_env=env),
        ReplaceTextTool(
            workspace=workspace, mode=mode, write_roots=write_roots, exec_env=env
        ),
        WriteFileTool(
            workspace=workspace,
            mode=mode,
            write_roots=write_roots,
            allowed_path_globs=write_path_globs,
            exec_env=env,
        ),
        ApplyPatchTool(
            workspace=workspace, mode=mode, write_roots=write_roots, exec_env=env
        ),
    ]
    if shell_mode is not ShellMode.OFF:
        tools.append(
            ShellRunTool(
                workspace=workspace,
                mode=shell_mode,
                rules=build_allowlist(
                    shell_allowlist, base_rules=DEFAULT_SHELL_RULES
                ),
                exec_env=env,
            )
        )
        # shell_poll rides with shell_run — it pulls the snapshot +
        # status of a background job the model started via shell_run.
        tools.append(ShellPollTool())
        # shell_kill lets the model stop a background job it
        # launched (SIGTERM→SIGKILL); high-risk, so PermissionGuard gates it.
        tools.append(ShellKillTool())
    return {t.name: t for t in tools}


# ``FsToolPack`` is the public name from the PRD; in I5 it equals the
# read + edit + shell builder.
FsToolPack = build_fs_tools

#: Provider-mutex edit-tool table (phase 2c): which of this pack's two
#: mutually-exclusive batch/precise edit tools the assembly layer must DROP
#: for a bound model's vendor family (Anthropic ships ``edit``, OpenAI /
#: GPT ships ``apply_patch``). The fs built-in owns the two names because it
#: ships both tools; the model→family judgment lives in the providers
#: built-in's catalog, and the kernel builder applies the drop mechanically
#: through its ``edit_tool_mutex`` injection (an unrecognised family drops
#: nothing — both stay, so existing recordings resume byte-equal).
PROVIDER_EDIT_TOOL_MUTEX: Mapping[str, tuple[str, ...]] = {
    "anthropic": ("apply_patch",),
    "openai": ("edit",),
}


def build_fs_session_pack(ctx: SessionBuildContext) -> PackContribution:
    """The fs pack as a ``session_pack`` contribution (microkernel phase 3).

    The manifest-declared factory (band 100) the kernel builder's generic
    loop calls. Builds the pack against the kernel-built workspace root,
    applies this plugin's own :data:`PROVIDER_EDIT_TOOL_MUTEX` (the
    edit↔apply_patch drop is fs knowledge — the kernel no longer carries the
    table), then filters by the agent whitelist — the base packs (fs / web)
    are the only ones that pass through ``allowed_tools``; capability packs
    append past it by design.

    The write/shell safety inputs are fs's own ``plugin_config["fs"]`` entry
    (mechanism-slots-only context, spec §4.2): the pack parses each key here,
    defaulting an absent key to the safe closure (``DRY_RUN`` writes /
    ``ALLOWLIST`` shell) exactly as the old builder kwargs did — the sole
    consumer of these knobs, so they never earned a typed context slot.
    """
    cfg = ctx.config("fs")
    pack = build_fs_tools(
        ctx.workspace,
        mode=cast(FsWriteMode, cfg.get("write_mode", FsWriteMode.DRY_RUN)),
        shell_mode=cast(ShellMode, cfg.get("shell_mode", ShellMode.ALLOWLIST)),
        shell_allowlist=cast(
            "Sequence[Mapping[str, Any]]", cfg.get("shell_allowlist", ())
        ),
        write_path_globs=cast(
            "tuple[str, ...]", cfg.get("write_path_globs", ())
        ),
        write_roots=cast(
            "Optional[WriteRootsResolver]", cfg.get("write_roots")
        ),
        exec_env=ctx.exec_env,
    )
    if ctx.provider_family is not None:
        for _drop in PROVIDER_EDIT_TOOL_MUTEX.get(ctx.provider_family, ()):
            pack.pop(_drop, None)
    return PackContribution(
        tools={
            name: tool
            for name, tool in pack.items()
            if name in ctx.allowed_tools
        }
    )
