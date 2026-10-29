"""What a :class:`~noeta.backends.execenvs.sandbox.LocalSandbox` command may
touch: the paths it may write, may not write, may not read and may read again
(:func:`plan_paths`), and that plan as a ``bwrap`` command line
(:func:`bwrap_args`, Linux) or a Seatbelt profile (:func:`seatbelt_profile`,
macOS)."""

from __future__ import annotations

import glob
import os
import subprocess
import sys
from collections.abc import Sequence
from dataclasses import dataclass, field

from noeta.toolkit.paths import noeta_home
from noeta.toolkit.protected_paths import PROTECTED_DIRS, PROTECTED_FILES, PROTECTED_NESTED
from noeta.toolkit.sandbox import SandboxSettings

#: Credential stores in the home directory a confined command never reads
#: unless ``allowRead`` names them.
DEFAULT_DENY_READ = (
    "~/.ssh", "~/.gnupg", "~/.aws", "~/.azure", "~/.config/gcloud", "~/.kube",
    "~/.docker/config.json", "~/.config/gh", "~/.netrc", "~/.git-credentials",
    "~/.pypirc", "~/.npmrc", "~/.cargo/credentials", "~/.cargo/credentials.toml",
    "~/.pgpass", "~/.password-store",
)
#: Inside noeta's home (otherwise unreadable: tokens, the task store), what
#: a command may still read: spilled tool output and project memory.
HOME_ALLOW_READ = ("tool-results", "projects")
#: Paths under a writable directory a confined command may not change: the
#: permissions plugin's protected directories and files (noeta's project
#: directory, editor tasks, hook runners, shell and package-manager
#: settings) and git's hooks and config — code that would later run outside
#: the sandbox. ``.git`` itself stays writable (git works in the sandbox) but
#: pinned. Only a missing :data:`PLACEHOLDERS` path is covered by an empty
#: read-only placeholder, so it cannot be created either.
PROTECTED = (
    *(d for d in PROTECTED_DIRS if d != ".git"),
    *("/".join(n) for n in PROTECTED_NESTED),
    *sorted(PROTECTED_FILES),
    ".git/hooks", ".git/config", ".git/config.worktree",
)
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
    ".noeta", ".git/hooks", ".git/config", ".git/config.worktree",
    ".npmrc", ".yarnrc", ".yarnrc.yml", ".yarn", ".pnpmfile.cjs", ".pnp.cjs",
    ".pnp.loader.mjs", "bunfig.toml", ".bunfig.toml", ".cargo", ".mvn", ".bazelrc",
    ".bazelversion", ".bazeliskrc"})
#: The :data:`PLACEHOLDERS` that are directories; the rest are files.
_PLACEHELD_DIRS = frozenset({".noeta", ".git/hooks", ".yarn", ".cargo", ".mvn"})
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


def _module_gitdirs(gitdir: str, limit: int = 256) -> list[str]:
    """The submodules' git directories under ``gitdir/modules`` (a name may
    hold slashes; a submodule may have its own)."""
    found: list[str] = []
    stack = [os.path.join(gitdir, "modules")]
    while stack and len(found) < limit:
        try:
            entries = list(os.scandir(stack.pop()))
        except OSError:
            continue
        for entry in entries:
            if not entry.is_dir(follow_symlinks=False):
                continue
            if os.path.isfile(os.path.join(entry.path, "HEAD")):
                found.append(entry.path)
                stack.append(os.path.join(entry.path, "modules"))
            else:
                stack.append(entry.path)
    return found


def _hooks_dir(project: str) -> str | None:
    """The hooks directory git runs from for ``project`` (``core.hooksPath``
    when set), or ``None`` when it is not a git work tree."""
    if not os.path.lexists(os.path.join(project, ".git")):
        return None
    try:
        done = subprocess.run(["git", "rev-parse", "--git-path", "hooks"], cwd=project,
                              capture_output=True, timeout=10, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return None
    out = done.stdout.decode("utf-8", "replace").strip()
    if done.returncode != 0 or not out:
        return None
    return os.path.realpath(os.path.join(project, out))


def _git_protected(project: str) -> list[str]:
    """What git runs code from in ``project`` beyond :data:`PROTECTED`: each
    submodule's config and hooks, and the configured hooks directory."""
    out: list[str] = []
    for module in _module_gitdirs(os.path.join(project, ".git")):
        out += [os.path.join(module, "hooks"),
                *(os.path.join(module, name) for name in _GIT_CONFIGS)]
    hooks = _hooks_dir(project)
    if hooks is not None:
        out.append(hooks)
    return out


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


def plan_paths(settings: SandboxSettings, root: str, tmpdir: str,
               writable: Sequence[str] = ()) -> Plan:
    """The paths of :class:`Plan` for a command whose working directory is
    ``root``."""
    base = root
    writes = [root, *_expand(settings, settings.allow_write, base), tmpdir, *writable]
    writes = [w for w in dict.fromkeys(os.path.realpath(w) for w in writes) if os.path.exists(w)]
    user_deny = [os.path.realpath(p) for p in _expand(settings, settings.deny_write, base)]
    # Placeholders go where a missing path matters: the denyWrite settings
    # and the protected paths of the working directories (not of TMPDIR or
    # allowWrite, which no one trusts as a project).
    projects = [os.path.realpath(p) for p in (root, *writable)]
    git_protected = [path for p in projects for path in _git_protected(p)
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
    home = str(noeta_home())
    deny_read = _expand(settings, [*DEFAULT_DENY_READ, home, *settings.deny_read], base)
    tenant_deny = _expand(settings, settings.tenant_deny_read, base)
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
    guarded = [*names, *git_protected]
    for path in [*user_deny, *guarded]:
        missing = _first_missing(path)
        if missing is not None and path in deny_write \
                and any(_within(missing, w) and missing != w for w in writes):
            placeholders.setdefault(missing, missing == path and (
                names.get(path, False) or ("/.git/" in path
                                           and os.path.basename(path) in _GIT_CONFIGS)))
    return Plan(writable=writes, deny_write=deny_write, deny_read=deny_read,
                allow_read=allow_read, pinned=pinned, placeholders=list(placeholders.items()),
                rebound=rebound)


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
    if not settings.filesystem_disabled:
        for path in plan.writable:
            args += ["--bind", path, path]
        hidden = [p for p in plan.deny_read if os.path.isdir(p)]
        for path in plan.deny_read:
            if path in hidden:
                args += ["--tmpfs", path]
            elif os.path.exists(path):
                args += ["--ro-bind", "/dev/null", path]
        # Re-opened paths are mounted while the empty directories over the
        # denied ones are still writable (their mount points), then those
        # turn read-only.
        for path in plan.allow_read:
            if os.path.exists(path) and any(_within(path, d) for d in hidden):
                args += ["--ro-bind", path, path]
        for path in sorted(plan.rebound, key=lambda p: p.count("/")):
            args += ["--bind", path, path]
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
    for path, _port in relays:
        args += ["--ro-bind", path, path]
    args += ["--ro-bind", bridge, bridge]
    for name in settings.deny_env:
        args += ["--unsetenv", name]
    args += ["--chdir", cwd, "--"]
    inner = [sys.executable, "-I", "-S", bridge]
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
        lines += [f"(deny file-read* (subpath {_sb_quote(os.path.realpath(p))}))"
                  for p in plan.deny_read]
        lines += [f"(allow file-read* (subpath {_sb_quote(os.path.realpath(p))}))"
                  for p in (*plan.allow_read, *plan.rebound)
                  if any(_within(p, d) for d in plan.deny_read)]
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
