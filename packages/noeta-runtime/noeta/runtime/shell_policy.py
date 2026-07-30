"""Shell policy — modes, the structural allowlist engine, the project rules file.

Kernel-band infrastructure shared by the ``shell_run`` tool (the fs pack), the
``run_skill_script`` tool, and the SDK host's approval predicate
(``command_in_allowlist``). The tool implementations live in the ``fs``
built-in plugin; this module owns the *mechanism*: :class:`ShellMode`, the
:class:`AllowRule` matching engine, spec parsing, and the per-project
remembered-rules file (``.noeta/shell-allowlist.json``). The *curated*
default rule table is product policy and ships with the fs built-in
(``noeta.builtins.fs.impl.shell_rules``, phase 2c) — callers hand it to
:func:`build_allowlist` as ``base_rules``; the kernel curates no commands.
"""

from __future__ import annotations

import json
import shlex
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Mapping, Optional, Sequence

from noeta.runtime.exec_env import ExecEnv


__all__ = [
    "AllowRule",
    "DEFAULT_SHELL_OUTPUT_CAP",
    "DEFAULT_SHELL_TIMEOUT_S",
    "MAX_SHELL_TIMEOUT_MS",
    "SHELL_META_CHARS",
    "ShellMode",
    "append_project_shell_rule",
    "build_allowlist",
    "command_in_allowlist",
    "load_project_shell_allowlist",
    "project_shell_allowlist_path",
    "rule_spec_from_command",
]


DEFAULT_SHELL_TIMEOUT_S = 120

#: ceiling for the per-call ``timeout`` argument (milliseconds),
#: mirroring Claude Code's Bash (max 600000ms / 10 min).
MAX_SHELL_TIMEOUT_MS = 600_000

#: Cap on captured stdout/stderr **bytes per stream**. Output past this
#: is dropped from the artifact too — the tool returns ``truncated=True``
#: so the agent knows to narrow its command (e.g. ``pytest -q``).
DEFAULT_SHELL_OUTPUT_CAP = 256 * 1024  # 256 KB

#: Bytes of stdout / stderr to embed inline in the ``ToolResult.output``.
#: The agent gets the **tail** (test failures land at the end of
#: pytest output); the full stream is the artifact.
_STDOUT_TAIL_BYTES = 2048
_STDERR_TAIL_BYTES = 1024

SHELL_META_CHARS = frozenset(";&|<>`$()\n\r")


class ShellMode(str, Enum):
    """Pre-run shell policy bound at ``FsToolPack`` construction (I4 maps
    CLI flags to this — ``--allow-shell`` ⇒ :attr:`ARBITRARY`).

    * :attr:`OFF` — ``shell_run`` is not in the pack at all.
    * :attr:`ALLOWLIST` — only the structural allowlist is permitted.
    * :attr:`ARBITRARY` — any non-shell-metachar command runs (high-risk,
      not for the daemon default Agent).
    """

    OFF = "off"
    ALLOWLIST = "allowlist"
    ARBITRARY = "arbitrary"


# ---------------------------------------------------------------------------
# Allowlist
# ---------------------------------------------------------------------------


_ArgValidator = Callable[[list[str]], bool]


@dataclass(frozen=True)
class AllowRule:
    """One structural allowlist entry.

    ``program`` matches ``argv[0]`` exactly; ``subcommand`` (if set)
    matches ``argv[1]`` exactly. ``validate`` then inspects the tail
    args. Matching is on the parsed argv only — never on the raw string.
    """

    program: str
    subcommand: Optional[str]
    validate: _ArgValidator
    label: str

    def matches(self, argv: list[str]) -> bool:
        if not argv or argv[0] != self.program:
            return False
        tail = argv[1:]
        if self.subcommand is not None:
            if not tail or tail[0] != self.subcommand:
                return False
            tail = tail[1:]
        return self.validate(tail)


def _trivial_validate(_: list[str]) -> bool:
    return True


def _rule_from_spec(spec: Mapping[str, Any]) -> AllowRule:
    """Convert one JSON-serializable allowlist spec → an :class:`AllowRule`.

    Spec shape (JSON-serializable, config-friendly)::

        {"program": "npm", "subcommand": "start"}   # subcommand optional

    Operator-configured rules use :func:`_trivial_validate`: any tail args are
    accepted *provided* the top-level shell-metachar scan passed (``; & | < > $``
    etc. are rejected before validation runs). This is deliberately looser than
    the curated built-in validators (which pin exact safe flag shapes) — a config
    rule means "this program/subcommand may run", not "with exactly these args".
    """
    program = spec.get("program")
    if not isinstance(program, str) or not program:
        raise ValueError(
            f"shell allowlist rule needs a non-empty string 'program': {spec!r}"
        )
    subcommand = spec.get("subcommand")
    if subcommand is not None and not isinstance(subcommand, str):
        raise ValueError(
            f"shell allowlist rule 'subcommand' must be a string or absent: {spec!r}"
        )
    label = spec.get("label")
    if not isinstance(label, str) or not label:
        label = program if subcommand is None else f"{program}_{subcommand}"
    return AllowRule(program, subcommand, _trivial_validate, label)


def build_allowlist(
    extra_specs: Sequence[Mapping[str, Any]] = (),
    *,
    base_rules: Sequence[AllowRule] = (),
) -> tuple[AllowRule, ...]:
    """``base_rules`` + operator-configured extra rules (extend).

    Phase 2c: the *curated* rule table (git status/diff, pytest, uv run
    pytest, npm/pnpm test, grep/rg/find/ls) is product policy and ships
    with the fs built-in (``noeta.builtins.fs.impl.shell_rules``); callers
    pass it as ``base_rules``. This kernel helper keeps only the mechanics:
    the base is kept verbatim and ``extra_specs`` from host config are
    parsed and appended. Empty base + empty extras ⇒ an empty allowlist
    (nothing runs in ALLOWLIST mode) — the kernel curates no commands.
    """
    return tuple(base_rules) + tuple(_rule_from_spec(s) for s in extra_specs)


def command_in_allowlist(command: str, rules: Sequence["AllowRule"]) -> bool:
    """True iff ``command`` is a well-formed, metachar-free argv matching a rule.

    Shared by the tool's own ALLOWLIST gate and the SDK-host approval predicate
    (which asks "does this need human sign-off?" = ``not command_in_allowlist``).
    A command with shell metacharacters or unbalanced quotes is never considered
    allowlisted (the tool rejects metas regardless of mode).
    """
    if _has_shell_meta(command):
        return False
    argv = _parse_argv(command)
    if not argv:
        return False
    return _matches_allowlist(argv, tuple(rules))


def rule_spec_from_command(command: str) -> Optional[dict[str, str]]:
    """Derive a persist-able allowlist spec from an approved command.

    Granularity is program + first arg: ``npm start`` -> ``{"program": "npm",
    "subcommand": "start"}``; a bare ``ls`` -> ``{"program": "ls"}``. Returns
    ``None`` for a command with metachars / unbalanced quotes (nothing safe to
    remember). The resulting rule accepts any tail args (``_trivial_validate``).
    """
    if _has_shell_meta(command):
        return None
    argv = _parse_argv(command)
    if not argv:
        return None
    spec: dict[str, str] = {"program": argv[0]}
    if len(argv) >= 2:
        spec["subcommand"] = argv[1]
    return spec


def project_shell_allowlist_path(workspace_root: Path) -> Path:
    """Per-project remembered-rules file: ``<workspace>/.noeta/shell-allowlist.json``."""
    return Path(workspace_root) / ".noeta" / "shell-allowlist.json"


def load_project_shell_allowlist(
    workspace_root: Path, *, exec_env: Optional[ExecEnv] = None
) -> tuple[dict[str, Any], ...]:
    """Load the project's remembered allowlist specs (empty if absent/malformed).

    Plain external config read - it never enters the LLM context or the event
    log; it just feeds the effective allowlist when the tools are built for a turn.

    ``exec_env`` (sandbox mode) reads the allowlist file THROUGH the container —
    ``workspace_root`` is then the container workdir, so the rules come from the
    file INSIDE the sandbox (this fixes the v1 bug where the loader read a
    container path against the host filesystem). ``None`` keeps the host read
    byte-identical.
    """
    path = project_shell_allowlist_path(workspace_root)
    try:
        if exec_env is not None:
            raw = json.loads(exec_env.read_text(path, encoding="utf-8"))
        else:
            raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeDecodeError):
        return ()
    if not isinstance(raw, list):
        return ()
    out: list[dict[str, Any]] = []
    for item in raw:
        if (
            isinstance(item, Mapping)
            and isinstance(item.get("program"), str)
            and item["program"]
        ):
            out.append(dict(item))
    return tuple(out)


def append_project_shell_rule(
    workspace_root: Path, spec: Mapping[str, Any]
) -> bool:
    """Append ``spec`` to the project allowlist file, deduped by (program, subcommand).

    Returns True if newly added, False if already present. Creates ``.noeta/`` as
    needed. Pure external side-effect (see :func:`load_project_shell_allowlist`).
    """
    path = project_shell_allowlist_path(workspace_root)
    existing = list(load_project_shell_allowlist(workspace_root))
    key = (spec.get("program"), spec.get("subcommand"))
    for entry in existing:
        if (entry.get("program"), entry.get("subcommand")) == key:
            return False
    existing.append(dict(spec))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(existing, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return True


def _has_shell_meta(command: str) -> bool:
    return any(c in SHELL_META_CHARS for c in command)


def _resolve_timeout(raw: Any, default_s: int) -> int:
    """Per-call ``timeout`` (milliseconds) → seconds, clamped to
    :data:`MAX_SHELL_TIMEOUT_MS`. Absent / non-positive / non-numeric falls
    back to the construction default. ``bool`` is excluded (it is an ``int``
    subclass but never a meaningful timeout)."""
    if isinstance(raw, bool) or not isinstance(raw, (int, float)) or raw <= 0:
        return default_s
    ms = min(int(raw), MAX_SHELL_TIMEOUT_MS)
    return max(1, ms // 1000)


def _parse_argv(command: str) -> Optional[list[str]]:
    try:
        return shlex.split(command, posix=True)
    except ValueError:
        return None


def _matches_allowlist(argv: list[str], rules: tuple[AllowRule, ...]) -> bool:
    return any(rule.matches(argv) for rule in rules)


# ---------------------------------------------------------------------------
# Process execution + output handling
# ---------------------------------------------------------------------------
#
# The run/capture/cap primitives (`run_argv` / `tail_bytes` / `cap_stream`
# / ``RunOutcome``) live in ``noeta.tools.fs._subprocess`` so ``shell_run``
# and ``run_skill_script`` share the exact timeout / truncation boundary.

