"""The ``fs`` plugin body: the tool classes plus the pack-construction factory.

The pack is closure-constructed — every tool is built holding the one
``WorkspaceRoot`` (the path-containment seam) and the write / shell mode it
must enforce, so the runtime never passes policy at call time and the ``Tool``
Protocol stays free of fs concepts. The policy types live in ``noeta.runtime``
so other consumers can depend on them without touching this body; nothing
imports this module statically, the plugin loader's ``ref`` resolution being
the only doorway.
"""

from __future__ import annotations

from typing import Any, Mapping, Optional, Sequence, cast

from noeta.builtins.fs.impl.edit import (
    WRITE_FILE_MAX_BYTES,
    ReplaceTextTool,
    WriteFileTool,
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
    "FsToolPack",
    "GlobTool",
    "GrepTool",
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

    Defaults are the safe closures — ``DRY_RUN`` writes (diff artifacts only,
    nothing lands on disk) and ``ALLOWLIST`` shell — so a host that forgets to
    opt in cannot mutate anything by accident.

    ``write_path_globs`` confines ``Write`` to workspace-relative paths matching
    one of the globs (empty ⇒ unrestricted); it deliberately does not constrain
    ``Edit``. ``write_roots`` is the host's authorization seam for writes
    OUTSIDE the workspace, consulted per call by ``Edit`` / ``Write``: ``None``
    keeps the single-root wall, while a host able to obtain a human grant
    passes a resolver so the approved directory is open on the resumed call.
    Reads are never fenced and ignore both.

    ``exec_env`` is the backend the pack's real IO routes through; a sandbox
    backend paired with a container ``workspace`` moves the whole pack into a
    container without altering any tool's name, schema, or description, so the
    stable prefix is unaffected.
    """
    # ``LocalExecEnv`` is stateless, so one instance is safely shared by the
    # whole pack.
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
        # Both act only on background jobs ``shell_run`` started, so they ride
        # with it instead of shipping independently.
        tools.append(ShellPollTool())
        tools.append(ShellKillTool())
    return {t.name: t for t in tools}


FsToolPack = build_fs_tools


def build_fs_session_pack(ctx: SessionBuildContext) -> PackContribution:
    """The fs pack as a ``session_pack`` contribution.

    The agent whitelist is applied here because the base packs (fs / web) are
    the only ones filtered by ``allowed_tools``; capability packs append past it
    by design. The write/shell safety inputs come from fs's own
    ``plugin_config["fs"]`` entry rather than a typed context slot — this pack
    is their sole consumer — and an absent key falls back to the safe closure.
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
    return PackContribution(
        tools={
            name: tool
            for name, tool in pack.items()
            if name in ctx.allowed_tools
        }
    )
