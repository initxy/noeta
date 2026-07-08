"""`noeta.tools.fs` — the file-system tool pack for Noeta Code (Phase 4).

The pack is *closure-constructed*: ``build_fs_tools`` takes one
``WorkspaceRoot`` (the path-containment seam), an ``FsWriteMode`` (the
``DRY_RUN`` / ``APPLY`` policy for the edit tools), and a ``ShellMode``
(the ``OFF`` / ``ALLOWLIST`` / ``ARBITRARY`` policy for shell + git
tools), and returns the dict of Tool instances keyed by their
provider-safe ``snake_case`` name. Each tool keeps a reference to the
workspace + its mode so the runtime never has to pass them in (the L0
``Tool`` Protocol stays unchanged).

The modes are bound at construction (B13): the CLI (I4) maps
``--allow-write`` / ``--allow-shell`` / ``--read-only`` flags into
single mode values *before* the Engine starts. There is no
"see-diff-then-apply" pause inside the Engine, and there is no run-time
re-negotiation of shell privileges.

* **I1** shipped the read-only tools — ``read`` / ``glob`` / ``grep``.
* **I2** added ``edit`` / ``write`` (rename of the
  former ``replace_text`` / ``write_file``) with the
  dry-run-by-default policy.
* **I5** adds ``shell_run`` with the ALLOWLIST-by-default policy.
  ``OFF`` removes ``shell_run`` entirely (the daemon default Agent).
"""

from __future__ import annotations

from typing import Any, Mapping, Optional, Sequence

from noeta.protocols.tool import Tool
from noeta.tools.fs._workspace import WorkspaceEscape, WorkspaceRoot
from noeta.tools.fs.exec_env import ExecEnv, LocalExecEnv
from noeta.tools.fs.edit import (
    WRITE_FILE_MAX_BYTES,
    FsWriteMode,
    ReplaceTextTool,
    WriteFileTool,
)
from noeta.tools.fs.patch import (
    MAX_PATCH_CANONICAL_BYTES,
    MAX_PATCH_EDITS,
    ApplyPatchTool,
)
from noeta.tools.fs.read import GlobTool, GrepTool, ReadFileTool
from noeta.tools.fs.shell import (
    DEFAULT_SHELL_OUTPUT_CAP,
    DEFAULT_SHELL_TIMEOUT_S,
    ShellKillTool,
    ShellMode,
    ShellPollTool,
    ShellRunTool,
    build_allowlist,
)
from noeta.tools.fs.skill_script import (
    SKILL_SCRIPT_TOOL_NAME,
    RunSkillScriptTool,
    is_skill_script_resource,
)


__all__ = [
    "ApplyPatchTool",
    "DEFAULT_SHELL_OUTPUT_CAP",
    "DEFAULT_SHELL_TIMEOUT_S",
    "FsToolPack",
    "MAX_PATCH_CANONICAL_BYTES",
    "MAX_PATCH_EDITS",
    "FsWriteMode",
    "GlobTool",
    "GrepTool",
    "ReadFileTool",
    "ReplaceTextTool",
    "RunSkillScriptTool",
    "SKILL_SCRIPT_TOOL_NAME",
    "ShellKillTool",
    "ShellMode",
    "ShellPollTool",
    "ShellRunTool",
    "WRITE_FILE_MAX_BYTES",
    "WorkspaceEscape",
    "WorkspaceRoot",
    "WriteFileTool",
    "build_fs_tools",
    "is_skill_script_resource",
]


def build_fs_tools(
    workspace: WorkspaceRoot,
    *,
    mode: FsWriteMode = FsWriteMode.DRY_RUN,
    shell_mode: ShellMode = ShellMode.ALLOWLIST,
    shell_allowlist: Sequence[Mapping[str, Any]] = (),
    write_path_globs: tuple[str, ...] = (),
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

    ``exec_env`` is the execution backend the fs / shell tools route their real
    IO through. ``None`` (default) ⇒ each tool builds its own ``LocalExecEnv``
    (byte-identical to the pre-seam host behaviour); a sandbox backend
    (:class:`~noeta.tools.fs.exec_env.AioSandboxExecEnv`) makes the whole pack
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
        ReplaceTextTool(workspace=workspace, mode=mode, exec_env=env),
        WriteFileTool(
            workspace=workspace,
            mode=mode,
            allowed_path_globs=write_path_globs,
            exec_env=env,
        ),
        ApplyPatchTool(workspace=workspace, mode=mode, exec_env=env),
    ]
    if shell_mode is not ShellMode.OFF:
        tools.append(
            ShellRunTool(
                workspace=workspace,
                mode=shell_mode,
                rules=build_allowlist(shell_allowlist),
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
