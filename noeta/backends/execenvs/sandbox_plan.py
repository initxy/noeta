"""What a :class:`~noeta.backends.execenvs.sandbox.LocalSandbox` command may
touch: the paths it may write, may not write, may not read and may read again
(:func:`plan_paths`), and that plan as a ``bwrap`` command line
(:func:`bwrap_args`, Linux) or a Seatbelt profile (:func:`seatbelt_profile`,
macOS)."""

from __future__ import annotations

import contextlib
import glob
import os
import subprocess
import sys
from collections.abc import Sequence
from dataclasses import dataclass, field

from noeta.toolkit.paths import noeta_home
from noeta.toolkit.protected_paths import PROTECTED_DIRS, PROTECTED_FILES, PROTECTED_NESTED
from noeta.toolkit.sandbox import DEFAULT_DENY_READ, SandboxSettings

#: Inside noeta's home (otherwise unreadable: tokens, the task store), what
#: a command may still read: spilled tool output and project memory.
HOME_ALLOW_READ = ("tool-results", "projects")
#: Paths under a writable directory a confined command may not change: the
#: permissions plugin's protected directories and files (noeta's project
#: directory, editor tasks, hook runners, shell and package-manager
#: settings) and git's hooks and config — code that would later run outside
#: the sandbox — with what points git at other ones: ``commondir`` (another
#: directory's config and hooks) and the submodules' git directories.
#: ``.git`` itself stays writable (git works in the sandbox) but pinned.
#: Only a missing :data:`PLACEHOLDERS` path is covered by an empty read-only
#: placeholder, so it cannot be created either.
PROTECTED = (
    *(d for d in PROTECTED_DIRS if d != ".git"),
    *("/".join(n) for n in PROTECTED_NESTED),
    *sorted(PROTECTED_FILES),
    ".git/hooks", ".git/config", ".git/config.worktree", ".git/commondir", ".git/modules",
)
#: Protected git files a placeholder cannot stand in for (git dies on an
#: empty ``commondir``, in the sandbox and on the host, which sees the
#: mount point): a missing one is removed when the command ends instead.
#: Seatbelt refuses creating it outright.
REMOVED_AFTER = (".git/commondir",)
#: The protected names a command must not create when missing: what is
#: executed on the next ordinary command without a confirmation step —
#: noeta's own directory, git's hooks and config, and the settings a
#: package manager or build tool runs code from (``yarnPath``, npm's
#: ``node-options``, a pnpmfile, bun's preload, cargo's and maven's
#: extensions, bazel's flags and the bazelisk download). The other protected
#: names are read-only when present but get no placeholder, since each runs
#: only after a separate step of the person's (``direnv allow``, an editor
#: trusting the workspace, installing a hook runner): a placeholder is an
#: empty entry the person sees appear in the project while a command runs.
#: The configured hooks directory is always placeheld, and a hook runner's
#: other configs (:data:`HOOK_RUNNER_CONFIGS`) once it is in use.
PLACEHOLDERS = frozenset({
    ".noeta", ".git/hooks", ".git/config", ".git/config.worktree", ".git/modules",
    ".npmrc", ".yarnrc", ".yarnrc.yml", ".yarn", ".pnpmfile.cjs", ".pnp.cjs",
    ".pnp.loader.mjs", "bunfig.toml", ".bunfig.toml", ".cargo", ".mvn", ".bazelrc",
    ".bazelversion", ".bazeliskrc"})
#: The :data:`PLACEHOLDERS` that are directories; the rest are files.
_PLACEHELD_DIRS = frozenset({".noeta", ".git/hooks", ".git/modules", ".yarn", ".cargo",
                             ".mvn"})
#: A hook runner's configs: when one is present the hooks run it, and a
#: missing one (a ``-local`` override) is read on the next commit.
HOOK_RUNNER_CONFIGS = ("lefthook.yml", ".lefthook.yml", "lefthook.yaml", ".lefthook.yaml",
                       "lefthook-local.yml", ".lefthook-local.yml", "lefthook-local.yaml",
                       ".lefthook-local.yaml")
#: Directories that stay writable but are mounted onto themselves: a mount
#: point cannot be renamed, so what is protected inside cannot be moved
#: away and recreated. Every directory between a working directory and a
#: protected path is pinned the same way.
PINNED = (".git",)
#: Git config files: a missing one is covered by an empty file, which git
#: reads as no settings (a directory there would break git), as is a
#: missing config file of a tool (an empty file is no settings; a directory
#: breaks pnpm). Any other missing path gets an empty directory, which git
#: does not list; a placeholder file at a repository's top is listed in the
#: command's view of ``.git/info/exclude``, so git does not list it either.
_GIT_CONFIGS = ("config", "config.worktree")
#: What a commit in a linked worktree writes in the repository's shared git
#: directory (its hooks, config and the rest stay read-only).
GIT_COMMON_WRITABLE = ("objects", "refs", "logs")
#: In a linked worktree's own git directory, what git reads to find the
#: shared one and its settings: redirected, they would run code later.
GIT_WORKTREE_PROTECTED = ("commondir", "gitdir", "config.worktree")


def _expand(settings: SandboxSettings, paths: Sequence[str], base: str) -> list[str]:
    out: list[str] = []
    for raw in paths:
        path = settings.resolve(raw, base)
        if any(c in path for c in "*?["):
            out += sorted(glob.glob(path, recursive=True))
        else:
            out.append(path)
    # bwrap mounts on the real path; a symlinked home would not resolve.
    return list(dict.fromkeys(os.path.realpath(p) for p in out))


def _within(path: str, root: str) -> bool:
    return path == root or path.startswith(root.rstrip("/") + "/")


@dataclass(frozen=True)
class Plan:
    """What one command may touch, as absolute paths.

    ``pinned`` directories are mounted onto themselves (still writable).
    ``placeholders`` are missing denied paths, each covered by an empty
    read-only directory (or file, when ``True``) that ``bwrap`` creates on
    the host as its mount point."""

    writable: list[str]
    deny_write: list[str]
    deny_read: list[str]
    allow_read: list[str]
    pinned: list[str] = field(default_factory=list)
    placeholders: list[tuple[str, bool]] = field(default_factory=list)
    #: Writable directories inside a hidden one, mounted again over it.
    rebound: list[str] = field(default_factory=list)
    #: Missing paths the command must not leave behind (:data:`REMOVED_AFTER`).
    remove_after: list[str] = field(default_factory=list)


def _first_missing(path: str) -> str | None:
    """The outermost missing component of ``path``, when its parent is a
    real directory; ``None`` when ``path`` exists or there is none."""
    if os.path.lexists(path):
        return None
    current = path
    while True:
        parent = os.path.dirname(current)
        if parent == current:
            return None
        if os.path.lexists(parent):
            real_dir = os.path.isdir(parent) and not os.path.islink(parent)
            return current if real_dir else None
        current = parent


def _hooks_dir(project: str) -> str | None:
    """The hooks directory git runs from for ``project`` (``core.hooksPath``
    when set), or ``None`` when it is not a git work tree."""
    if not os.path.lexists(os.path.join(project, ".git")):
        return None
    try:
        done = subprocess.run(["git", "-c", "core.fsmonitor=", "rev-parse", "--git-path",
                               "hooks"], cwd=project,
                              capture_output=True, timeout=10, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return None
    out = done.stdout.decode("utf-8", "replace").strip()
    if done.returncode != 0 or not out:
        return None
    return os.path.realpath(os.path.join(project, out))


def _git_protected(project: str) -> list[str]:
    """What git runs code from in ``project`` beyond :data:`PROTECTED`: in
    each linked worktree's git directory what points git elsewhere (present
    ones), and the configured hooks directory."""
    out: list[str] = []
    worktrees = os.path.join(project, ".git", "worktrees")
    with contextlib.suppress(OSError):
        for entry in os.scandir(worktrees):
            if entry.is_dir(follow_symlinks=False):
                out += [path for name in GIT_WORKTREE_PROTECTED
                        if os.path.lexists(path := os.path.join(entry.path, name))]
    hooks = _hooks_dir(project)
    if hooks is not None:
        out.append(hooks)
    return out


def _read_line(path: str) -> str | None:
    try:
        with open(path, encoding="utf-8") as f:
            return f.readline().strip()
    except (OSError, UnicodeDecodeError):
        return None


def _linked_git_dirs(project: str) -> tuple[list[str], list[str]]:
    """``(writable, protected)`` git paths outside ``project`` when it is a
    linked worktree (``.git`` a file): its own git directory and the shared
    directories a commit writes; and in its own git directory what points
    git elsewhere. Only a git directory that is listed under the shared
    one's ``worktrees/`` and names ``project`` back counts, so a ``.git``
    file cannot open an arbitrary directory. Read as files: no git runs."""
    dotgit = os.path.join(project, ".git")
    if not os.path.isfile(dotgit) or os.path.islink(dotgit):
        return [], []
    line = _read_line(dotgit)
    if not line or not line.startswith("gitdir:"):
        return [], []
    gitdir = os.path.realpath(os.path.join(project, line[len("gitdir:"):].strip()))
    common_rel = _read_line(os.path.join(gitdir, "commondir"))
    back = _read_line(os.path.join(gitdir, "gitdir"))
    if not common_rel or not back:
        return [], []
    common = os.path.realpath(os.path.join(gitdir, common_rel))
    if os.path.dirname(gitdir) != os.path.join(common, "worktrees") \
            or os.path.realpath(os.path.join(gitdir, back)) != os.path.realpath(dotgit):
        return [], []
    shared = [os.path.join(common, n) for n in GIT_COMMON_WRITABLE]
    writable = [gitdir, *(p for p in shared if os.path.isdir(p) and not os.path.islink(p))]
    return writable, [os.path.join(gitdir, n) for n in GIT_WORKTREE_PROTECTED]


def _hook_runner_configs(project: str) -> tuple[str, ...]:
    """:data:`HOOK_RUNNER_CONFIGS` when one of them is in ``project``."""
    present = any(os.path.lexists(os.path.join(project, n)) for n in HOOK_RUNNER_CONFIGS)
    return HOOK_RUNNER_CONFIGS if present else ()


def exclude_overlays(projects: Sequence[str],
                     placeholders: Sequence[tuple[str, bool]]) -> list[tuple[str, str]]:
    """``(path, content)``: each project's ``.git/info/exclude`` as a
    command sees it, listing the placeholder files at the project's top so
    git neither lists nor adds them."""
    out: list[tuple[str, str]] = []
    for project in projects:
        names = sorted(os.path.basename(path) for path, is_file in placeholders
                       if is_file and os.path.dirname(path) == project)
        exclude = os.path.join(project, ".git", "info", "exclude")
        if not names or not os.path.isfile(exclude) or os.path.islink(exclude):
            continue
        try:
            with open(exclude, encoding="utf-8", errors="surrogateescape") as f:
                text = f.read()
        except OSError:
            continue
        if text and not text.endswith("\n"):
            text += "\n"
        out.append((exclude, text + "".join(f"/{n}\n" for n in names)))
    return out


def _between(path: str, writes: Sequence[str]) -> list[str]:
    """The real directories strictly between a writable directory and
    ``path``, outermost first."""
    for w in writes:
        if path != w and _within(path, w):
            parts = os.path.relpath(path, w).split(os.sep)[:-1]
            dirs = [os.path.join(w, *parts[:i]) for i in range(1, len(parts) + 1)]
            return [d for d in dirs if os.path.isdir(d) and not os.path.islink(d)]
    return []


def _tenant_refusal(path: str, roots: Sequence[str], denied: Sequence[str]) -> str | None:
    """Why a tenant's command may not write the resolved ``path``: it lies
    outside ``roots`` (the tenant's workspace), or in a ``denied`` place
    inside the root that holds it (one holding the whole root is the
    workspace's surroundings, hidden with the root shown again)."""
    holders = [r for r in roots if _within(path, r)]
    if not holders:
        return f"{path}, outside the tenant's workspace"
    for d in denied:
        if _within(path, d) and not any(_within(r, d) for r in holders):
            return f"{path}, inside {d}, which the tenant's commands may not read"
    return None


def plan_paths(settings: SandboxSettings, root: str, tmpdir: str,
               writable: Sequence[str] = ()) -> Plan:
    """The paths of :class:`Plan` for a command whose working directory is
    ``root``. For a tenant's task (``settings.tenant_write_roots``) raises
    ``PermissionError`` when ``root`` resolves outside its workspace or
    into a place it may not read, rather than make that place writable."""
    base = root
    home = str(noeta_home())
    deny_read = _expand(settings, [*DEFAULT_DENY_READ, home, *settings.deny_read], base)
    tenant_deny = _expand(settings, settings.tenant_deny_read, base)
    projects = [os.path.realpath(p) for p in (root, *writable)]
    roots = None if settings.tenant_write_roots is None \
        else [os.path.realpath(r) for r in settings.tenant_write_roots]
    guarded = [*tenant_deny, os.path.realpath(home)]
    if roots is not None:
        why = _tenant_refusal(projects[0], roots, guarded)
        if why is not None:
            raise PermissionError(f"the sandbox may not run this command: its working "
                                  f"directory {root} resolves to {why}")
    # A linked worktree's git data (git commits there), unless it lies in a
    # directory the command may not read (or, for a tenant's task, outside
    # its workspace).
    linked = [_linked_git_dirs(p) for p in projects]
    linked_writes = [w for ws, _ in linked for w in ws
                     if not any(_within(w, d) for d in (*deny_read, *tenant_deny))
                     and (roots is None or _tenant_refusal(w, roots, guarded) is None)]
    writes = [root, *_expand(settings, settings.allow_write, base), tmpdir, *writable,
              *linked_writes]
    writes = [w for w in dict.fromkeys(os.path.realpath(w) for w in writes) if os.path.exists(w)]
    user_deny = [os.path.realpath(p) for p in _expand(settings, settings.deny_write, base)]
    # Placeholders go where a missing path matters: the denyWrite settings
    # and the protected paths of the working directories (not of TMPDIR or
    # allowWrite, which no one trusts as a project).
    git_protected = [path for p in projects for path in _git_protected(p)
                     if any(_within(path, w) for w in writes)]
    git_protected += [path for _, ps in linked for path in ps
                      if any(_within(path, w) for w in writes)]
    protected = [*(os.path.join(p, n) for p in projects for n in PROTECTED), *git_protected]
    deny_write = [*user_deny, *protected]
    pinned: list[str] = []
    for w in writes:
        for name in PINNED:
            path = os.path.join(w, name)
            if os.path.isdir(path) and not os.path.islink(path):
                pinned.append(path)
            elif os.path.lexists(path):
                deny_write.append(path)  # a worktree's ``.git`` file
        deny_write += [os.path.join(w, p) for p in PROTECTED]
    readable = HOME_ALLOW_READ if settings.noeta_home_readable is None \
        else settings.noeta_home_readable
    allow_read = _expand(settings, [*(os.path.join(home, p) for p in readable),
                                    *settings.allow_read, *settings.tenant_allow_read],
                         base)
    # A denied directory holding a writable one would hide it; a tenant's
    # stays denied and the writable one is mounted again inside it.
    deny_read = [d for d in deny_read if not any(_within(w, d) for w in writes)]
    deny_read += [d for d in tenant_deny if d not in deny_read and d not in writes]
    hidden = [d for d in deny_read if os.path.isdir(d)]
    rebound = [w for w in writes if any(_within(w, d) for d in hidden)]
    # A denied path inside a hidden directory is hidden already, unless a
    # directory shown again inside that one holds it.
    shown = [*(a for a in allow_read if any(_within(a, d) for d in hidden)), *rebound]
    deny_read = [d for d in deny_read if not any(
        d != h and _within(d, h) and not any(_within(d, s) and s != h and _within(s, h)
                                             for s in shown) for h in hidden)]
    hidden = [d for d in deny_read if os.path.isdir(d)]
    # Inside a hidden directory a path is unreadable and read-only already;
    # binding it would show it again (unless it is in a writable one shown
    # again).
    deny_write = [p for p in dict.fromkeys(deny_write)
                  if not any(_within(p, d) for d in hidden)
                  or any(_within(p, w) for w in rebound)]
    for path in deny_write:
        pinned += _between(path, writes)
    pinned = sorted(dict.fromkeys(p for p in pinned if p not in deny_write),
                    key=lambda p: p.count("/"))
    placeholders: dict[str, bool] = {}
    names = {os.path.join(p, n): n not in _PLACEHELD_DIRS
             for p in projects for n in (*PLACEHOLDERS, *_hook_runner_configs(p))}
    # A linked worktree's missing files are files git reads.
    names.update({path: True for _, ps in linked for path in ps})
    guarded = [*names, *git_protected]
    for path in [*user_deny, *guarded]:
        missing = _first_missing(path)
        if missing is not None and path in deny_write \
                and any(_within(missing, w) and missing != w for w in writes):
            placeholders.setdefault(missing, missing == path and (
                names.get(path, False) or ("/.git/" in path
                                           and os.path.basename(path) in _GIT_CONFIGS)))
    remove_after = [path for p in projects for n in REMOVED_AFTER
                    if os.path.isdir(os.path.dirname(path := os.path.join(p, n)))
                    and not os.path.lexists(path) and any(_within(path, w) for w in writes)]
    return Plan(writable=writes, deny_write=deny_write, deny_read=deny_read,
                allow_read=allow_read, pinned=pinned, placeholders=list(placeholders.items()),
                rebound=rebound, remove_after=remove_after)


def bwrap_args(settings: SandboxSettings, plan: Plan, *, bwrap: str, cwd: str,
               relays: Sequence[tuple[str, int]], bridge: str, empty: str = "/dev/null",
               overlays: Sequence[tuple[str, str]] = ()) -> list[str]:
    """The ``bwrap`` command line for ``plan``; the command follows it.
    ``empty`` is the empty file a placeholder file shows; ``overlays`` are
    ``(source, path)`` files shown read-only over ``path``."""
    args = [bwrap, "--die-with-parent", "--new-session", "--unshare-pid", "--unshare-net",
            "--unshare-ipc", "--unshare-uts"]
    if settings.filesystem_disabled:
        args += ["--bind", "/", "/"]
    else:
        args += ["--ro-bind", "/", "/"]
    args += ["--dev", "/dev"]
    args += ["--ro-bind", "/proc", "/proc"] if settings.weaker_nested else ["--proc", "/proc"]
    own = [a for path, _port in relays for a in ("--ro-bind", path, path)]
    own += ["--ro-bind", bridge, bridge]
    if not settings.filesystem_disabled:
        for path in plan.writable:
            args += ["--bind", path, path]
        hidden = [p for p in plan.deny_read if os.path.isdir(p)]
        # Outermost first, so a path denied inside a directory shown again
        # inside a hidden one is hidden after it is shown. Re-opened paths
        # are mounted while the empty directories over the denied ones are
        # still writable (their mount points), then those turn read-only.
        steps: list[tuple[str, int, list[str]]] = []
        for path in plan.deny_read:
            if path in hidden:
                steps.append((path, 0, ["--tmpfs", path]))
            elif os.path.exists(path):
                steps.append((path, 0, ["--ro-bind", "/dev/null", path]))
        steps += [(path, 1, ["--ro-bind", path, path]) for path in plan.allow_read
                  if os.path.exists(path) and any(_within(path, d) for d in hidden)]
        steps += [(path, 2, ["--bind", path, path]) for path in plan.rebound]
        for _path, _kind, step in sorted(steps, key=lambda s: (s[0].count("/"), s[1])):
            args += step
        # The bridge may lie in a hidden directory (a tenant's task hides
        # the server's own directories): mounted before those turn read-only.
        args += own
        for path in hidden:
            args += ["--remount-ro", path]
        for path in plan.pinned:
            args += ["--bind", path, path]
        for path in plan.deny_write:
            if os.path.exists(path):
                args += ["--ro-bind", path, path]
        for path, is_file in plan.placeholders:
            if is_file:
                args += ["--ro-bind", empty, path]
            else:
                args += ["--tmpfs", path, "--remount-ro", path]
        for source, path in overlays:
            args += ["--ro-bind", source, path]
    if settings.filesystem_disabled:
        args += own
    for name in settings.deny_env:
        args += ["--unsetenv", name]
    args += ["--chdir", cwd, "--"]
    # A tenant's task hides the server's own directories, which a virtual
    # environment's link to its Python may pass through: the Python itself
    # is shown.
    python = os.path.realpath(sys.executable) if settings.tenant_deny_read else sys.executable
    inner = [python, "-I", "-S", bridge]
    for kind, (path, port) in zip(("--http", "--socks"), relays, strict=False):
        inner += [kind, path, str(port)]
    if settings.allow_all_unix_sockets:
        inner.append("--allow-unix")
    if settings.nested_user_namespaces:
        inner.append("--allow-userns")
    return [*args, *inner, "--"]


def _sb_quote(path: str) -> str:
    return '"' + path.replace("\\", "\\\\").replace('"', '\\"') + '"'


def seatbelt_profile(settings: SandboxSettings, plan: Plan,
                     proxy_ports: Sequence[int]) -> str:
    """The macOS Seatbelt profile for ``plan``: later rules win."""
    lines = ["(version 1)", "(allow default)"]
    if not settings.filesystem_disabled:
        lines.append("(deny file-write*)")
        lines += [f"(allow file-write* (subpath {_sb_quote(os.path.realpath(p))}))"
                  for p in plan.writable]
        lines.append('(allow file-write* (literal "/dev/null") (literal "/dev/zero") '
                     '(regex #"^/dev/tty") (regex #"^/dev/fd/"))')
        reads = [(p, 0, "deny") for p in plan.deny_read]
        reads += [(p, 1, "allow") for p in (*plan.allow_read, *plan.rebound)
                  if any(_within(p, d) for d in plan.deny_read)]
        # Later rules win: outermost first, as for bwrap.
        lines += [f"({verb} file-read* (subpath {_sb_quote(os.path.realpath(p))}))"
                  for p, _kind, verb in sorted(reads, key=lambda r: (r[0].count("/"), r[1]))]
        lines += [f"(deny file-write* (subpath {_sb_quote(os.path.realpath(p))}))"
                  for p in plan.deny_write]
        # A subpath rule does not stop renaming a directory above it.
        lines += [f"(deny file-write* (literal {_sb_quote(os.path.realpath(p))}))"
                  for p in plan.pinned]
    lines.append("(deny network*)")
    lines += [f'(allow network-outbound (remote ip "localhost:{port}"))'
              for port in proxy_ports]
    if settings.allow_local_binding:
        lines.append('(allow network-bind (local ip "localhost:*"))')
        lines.append('(allow network-inbound (local ip "localhost:*"))')
        lines.append('(allow network-outbound (remote ip "localhost:*"))')
    if settings.allow_all_unix_sockets:
        lines.append("(allow network* (remote unix-socket))")
    else:
        lines += [f"(allow network* (remote unix-socket (path-literal {_sb_quote(p)})))"
                  for p in settings.allow_unix_sockets]
    return "\n".join(lines) + "\n"
