"""Git repository sync: shallow clone/fetch via the git CLI + optional token
auth + INDEX.md generation.

Usage (as a library function):
    from noeta.agent.services.knowledge.repo_sync import sync_repo
    sync_repo(
        git_url="https://git.example.com/org/repo.git",
        out_dir="/path/to/shared/knowledge/space123/src456/",
        token="glpat-...",
        branch="main",
        depth=1,
    )

Credential handling: the token is injected into the https URL as basic-auth
userinfo (``https://oauth2:<token>@host/…`` — the ``oauth2`` username +
token-as-password form is accepted by the common hosts) for the individual
clone/fetch invocation only. It must never be logged and never persist into
the materialized tree:

- git stores the clone URL verbatim in .git/config, so right after a clone
  the origin remote is rewritten back to the clean URL;
- incremental fetches pass the credentialed URL on the command line (never
  through the origin remote), so nothing is written to .git/config;
- error messages carry only the git subcommand name plus stderr scrubbed of
  the token and of any URL userinfo — they end up in last_error, which is
  readable by space members.
"""
from __future__ import annotations

import logging
import os
import re
import subprocess
from typing import Optional

logger = logging.getLogger(__name__)

#: Timeout (seconds) for clone/checkout of large repositories: even a shallow
#: clone of a big monorepo can take minutes.
_CLONE_TIMEOUT = 1800

#: URL userinfo (``scheme://user:pass@host``) scrub for error messages: a
#: generic backstop on top of the exact-token replacement.
_URL_CRED_RE = re.compile(r"://[^/@\s]+@")


class RepoSyncError(Exception):
    """A git operation failed."""


def _scrub(text: str, token: Optional[str]) -> str:
    """Remove the token and any URL-embedded credentials from an error
    message before it can propagate (last_error is member-readable)."""
    if token:
        text = text.replace(token, "***")
    return _URL_CRED_RE.sub("://***@", text)


def _run_git(
    args: list[str],
    cwd: Optional[str] = None,
    timeout: int = 300,
    token: Optional[str] = None,
) -> str:
    """Run a git command and return stdout; raise RepoSyncError on failure.

    The error message carries only the subcommand name and scrubbed stderr —
    never the joined args, which may contain the credentialed URL.
    """
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
    subcommand = args[0] if args else "?"
    try:
        r = subprocess.run(
            ["git", *args],
            cwd=cwd, env=env,
            capture_output=True, text=True, timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        raise RepoSyncError(f"git {subcommand} timed out (>{timeout}s)")
    if r.returncode != 0:
        err = _scrub((r.stderr or r.stdout or "").strip(), token)[:500]
        raise RepoSyncError(f"git {subcommand} failed: {err}")
    return r.stdout.strip()


def _inject_token(git_url: str, token: Optional[str]) -> str:
    """Inject the token into an http(s) URL as basic-auth userinfo.

    ``oauth2`` as the username with the token as the password is accepted by
    the common git hosts; non-http URLs are returned unchanged (no token
    transport there).
    """
    if not token:
        return git_url
    for scheme in ("https://", "http://"):
        if git_url.startswith(scheme):
            return f"{scheme}oauth2:{token}@{git_url[len(scheme):]}"
    return git_url


def _clone(
    git_url: str,
    out_dir: str,
    branch: Optional[str],
    depth: int,
    token: Optional[str],
) -> None:
    """Initial clone.

    With a branch configured, clone that branch; if it does not exist, fall
    back to the default branch. Right after the clone the origin remote is
    rewritten to the clean URL so the token never persists in .git/config.
    """
    auth_url = _inject_token(git_url, token)
    fallback_args = ["clone", "--depth", str(depth), auth_url, out_dir]
    if branch:
        clone_args = [
            "clone", "--branch", branch, "--depth", str(depth), auth_url, out_dir,
        ]
        try:
            _run_git(clone_args, timeout=_CLONE_TIMEOUT, token=token)
        except RepoSyncError:
            # The configured branch does not exist; try the default branch
            logger.info("repo_sync: branch %s not found, trying the default branch", branch)
            _run_git(fallback_args, timeout=_CLONE_TIMEOUT, token=token)
    else:
        _run_git(fallback_args, timeout=_CLONE_TIMEOUT, token=token)

    # Scrub the persisted credential: git wrote the credentialed clone URL
    # into .git/config; point origin back at the clean URL.
    if token:
        _run_git(["remote", "set-url", "origin", git_url], cwd=out_dir)


def _count_files(directory: str) -> int:
    """Recursively count files (excluding .git)."""
    count = 0
    git_dir = os.path.join(directory, ".git")
    for _root, dirs, files in os.walk(directory):
        if _root.startswith(git_dir):
            continue
        count += len(files)
    return count


def _generate_index_md(
    out_dir: str,
    git_url: str,
    branch: str,
    commit: str,
    file_count: int,
) -> None:
    """Generate the INDEX.md metadata file, shaped identically across source
    types so the agent's orientation blurb stays uniform."""
    lines = [
        "# Code repository index\n",
        f"_Repository: {git_url}_",
        f"_Branch: {branch}_",
        f"_Commit: {commit[:12]}_",
        f"_Files: {file_count}_",
    ]
    lines.append("")
    lines.append("## Retrieval hints\n")
    lines.append("- Use `rg <keyword>` for exact searches by name")
    lines.append(
        "- The repository has no directory overview; search directly by"
        " symbol / class / function name"
    )
    lines.append(f"- Repository origin: {git_url}")

    index_path = os.path.join(out_dir, "INDEX.md")
    with open(index_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


def sync_repo(
    git_url: str,
    out_dir: str,
    token: Optional[str] = None,
    branch: Optional[str] = None,
    depth: int = 1,
    progress_callback=None,
) -> dict:
    """Sync a git repository into a local directory.

    The first run clones; subsequent runs fetch + reset.

    Args:
        git_url: repository HTTPS URL (clean; the token is injected per
            invocation and never stored)
        out_dir: output directory (parent directories created automatically)
        token: access token for private repositories (optional)
        branch: branch name (None = the remote's default branch)
        depth: clone/fetch depth (1 = shallow clone)
        progress_callback: optional callback receiving dict status reports

    Returns:
        dict: {file_count, commit, branch}
    """
    out_dir = os.path.abspath(out_dir)
    os.makedirs(out_dir, exist_ok=True)

    is_existing = os.path.isdir(os.path.join(out_dir, ".git"))

    if progress_callback:
        progress_callback({"phase": "starting", "existing": is_existing})

    if not is_existing:
        # --- initial clone ---
        logger.info(
            "repo_sync: clone %s -> %s (branch=%s, depth=%d)",
            git_url, out_dir, branch or "(default)", depth,
        )

        _clone(git_url, out_dir, branch, depth, token)

        if progress_callback:
            progress_callback({"phase": "cloned"})
    else:
        # --- incremental fetch + reset ---
        logger.info("repo_sync: fetch + reset %s (branch=%s)", out_dir, branch or "(default)")

        # Fetch straight from the credentialed URL on the command line —
        # origin stays clean in .git/config, and FETCH_HEAD carries the
        # result. Long timeout: a large/private repository re-sync fails or
        # times out otherwise.
        auth_url = _inject_token(git_url, token)
        fetch_args = ["fetch", "--depth", str(depth), auth_url]
        if branch:
            fetch_args.append(branch)
        _run_git(fetch_args, cwd=out_dir, timeout=_CLONE_TIMEOUT, token=token)

        _run_git(
            ["reset", "--hard", "FETCH_HEAD"],
            cwd=out_dir, timeout=_CLONE_TIMEOUT, token=token,
        )

        if progress_callback:
            progress_callback({"phase": "fetched"})

    # Read the current commit
    commit = _run_git(["rev-parse", "HEAD"], cwd=out_dir)
    actual_branch = _run_git(["rev-parse", "--abbrev-ref", "HEAD"], cwd=out_dir)
    file_count = _count_files(out_dir)

    # Generate INDEX.md
    _generate_index_md(out_dir, git_url, actual_branch, commit, file_count)

    logger.info(
        "repo_sync finished: branch=%s commit=%s files=%d",
        actual_branch, commit[:12], file_count,
    )

    if progress_callback:
        progress_callback({"phase": "done", "file_count": file_count, "commit": commit})

    return {"file_count": file_count, "commit": commit, "branch": actual_branch}
