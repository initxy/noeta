"""The ``git`` function a command that only reads with ``git`` runs under:
a repository's own config, hooks and helper programs run nothing, and a
repository planted in the workspace is refused."""

from __future__ import annotations

import shlex

from noeta.core.tool import ToolContext
from noeta.toolkit.shell_syntax import parse
from noeta.toolkit.tools import Workspace

#: ``git`` subcommands that only read (those the permissions plugin can let
#: through without asking).
_GIT_READ_SUBCOMMANDS = frozenset(
    "log show diff whatchanged shortlog status ls-files ls-tree blame rev-parse cat-file "
    "describe grep reflog merge-base name-rev count-objects branch tag remote config "
    "stash".split())
#: Global ``git`` options whose value is the next word.
_GIT_GLOBAL_VALUED = ("-C", "-c", "--git-dir", "--work-tree", "--namespace", "--config-env",
                      "--super-prefix")
#: Settings a reading ``git`` runs with over any repository's own: no
#: fsmonitor hook, hooks, pager, signature program or transport (a lazy
#: fetch), and (git 2.38+) no repository found by walking into a bare one.
_GIT_SAFE_CONFIG = ("core.fsmonitor=", "core.hooksPath=/dev/null", "core.pager=cat",
                    "safe.bareRepository=explicit", "protocol.allow=never",
                    "log.showSignature=false", "gpg.program=false",
                    "gpg.openpgp.program=false", "gpg.x509.program=false",
                    "gpg.ssh.program=false")
#: A ``git`` function every reading command runs under: it runs ``git``
#: with :data:`_GIT_SAFE_CONFIG` and without external diff programs or
#: ``textconv`` filters, and with a guard (:data:`_GIT_GUARD`) refuses a
#: repository planted in the workspace.
_GIT_FUNCTION = """git() {
  local _noeta_pre=() _noeta_sub=() _noeta_dir _noeta_top _noeta_named
  while [ "$#" -gt 0 ]; do
    case "$1" in
      @VALUED@) [ "$#" -ge 2 ] || break; _noeta_pre+=("$1" "$2"); shift 2 ;;
      -*) _noeta_pre+=("$1"); shift ;;
      *) break ;;
    esac
  done
@GUARD@  case "${1-}" in
    diff|log|show|whatchanged) _noeta_sub=("$1" --no-ext-diff --no-textconv); shift ;;
    blame) _noeta_sub=(blame --no-textconv); shift ;;
    stash) if [ "${2-}" = show ]; then
        _noeta_sub=(stash show --no-ext-diff --no-textconv); shift 2; fi ;;
    reflog) case "${2-}" in
        expire|delete|drop|exists|list) ;;
        show) _noeta_sub=(reflog show --no-ext-diff --no-textconv); shift 2 ;;
        *) _noeta_sub=(reflog show --no-ext-diff --no-textconv); shift ;;
      esac ;;
  esac
  command git "${_noeta_pre[@]+"${_noeta_pre[@]}"}" @CONFIG@ \\
    "${_noeta_sub[@]+"${_noeta_sub[@]}"}" "$@"
}
""".replace("@VALUED@", "|".join(_GIT_GLOBAL_VALUED)).replace(
    "@CONFIG@", " ".join(f"-c {c}" for c in _GIT_SAFE_CONFIG))
#: The refusal of a repository whose git directory lies under the guard
#: root (``@ROOT@``) without being a ``.git`` directory or the one a
#: ``.git`` file at its top names: a bare repository committed into the
#: workspace, whose config its author wrote. A clone cannot carry a
#: ``.git`` file, so a worktree's or a separate git directory passes.
_GIT_GUARD = """  if _noeta_dir=$(command git "${_noeta_pre[@]+"${_noeta_pre[@]}"}" \\
      -c core.fsmonitor= rev-parse --absolute-git-dir 2>/dev/null) \\
      && _noeta_dir=$(cd "$_noeta_dir" 2>/dev/null && pwd -P); then
    case "$_noeta_dir/" in
      @ROOT@/*) case "$_noeta_dir" in
        */.git|*/.git/*) ;;
        *) _noeta_top=$(command git "${_noeta_pre[@]+"${_noeta_pre[@]}"}" -c core.fsmonitor= \\
             rev-parse --show-toplevel 2>/dev/null) || _noeta_top=
          _noeta_named=
          if [ -n "$_noeta_top" ] && [ -f "$_noeta_top/.git" ]; then
            _noeta_named=$(cd "$(command git -C "$_noeta_top" -c core.fsmonitor= \\
              rev-parse --absolute-git-dir 2>/dev/null)" 2>/dev/null && pwd -P)
          fi
          if [ "$_noeta_named" != "$_noeta_dir" ]; then
            printf 'git: refused the repository at %s: not a .git directory\\n' \\
              "$_noeta_dir" >&2; return 128
          fi ;;
      esac ;;
    esac
  fi
"""


def git_prelude(command: str, guard_root: str = "") -> str:
    """:data:`_GIT_FUNCTION` when ``command`` runs ``git`` only to read
    (every ``git`` in it has a :data:`_GIT_READ_SUBCOMMANDS` subcommand),
    so whatever directory it reads in, a repository's config runs nothing;
    ``""`` otherwise (a ``git commit`` keeps its hooks). With
    ``guard_root`` (a canonical directory) it also carries
    :data:`_GIT_GUARD` for that root."""
    parsed = parse(command)
    if parsed.opaque:
        return ""
    found = False
    for cmd in parsed.commands:
        argv = cmd.argv
        if not argv or argv[0] != "git":
            continue
        found = True
        i = 1
        while i < len(argv) and argv[i].startswith("-"):
            i += 2 if argv[i] in _GIT_GLOBAL_VALUED else 1
        if i >= len(argv) or argv[i] not in _GIT_READ_SUBCOMMANDS:
            return ""
    if not found:
        return ""
    guard = _GIT_GUARD.replace("@ROOT@", shlex.quote(guard_root)) if guard_root else ""
    return _GIT_FUNCTION.replace("@GUARD@", guard)


def guard_root(ws: Workspace, ctx: ToolContext) -> str:
    """The root :func:`git_prelude` refuses planted repositories under: the
    workspace, unless a person approved this command (it then runs
    hardened, as approved)."""
    return "" if ctx.approved else ws.root
