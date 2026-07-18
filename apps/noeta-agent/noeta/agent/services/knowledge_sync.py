"""Knowledge-source sync manager: in-process orchestration; a threadpool
runs background threads.

Sync flow:
1. The API receives a POST sync request → KnowledgeSyncManager.start_sync()
2. threadpool.submit(_run_sync, source_id) starts a background thread
3. The thread runs the sync adapter (git_repo or local_dir) and writes into
   shared_data_dir
4. The thread updates the DB status (syncing → ready/failed) and last_error

When the backend restarts, stale syncing statuses are cleared by
store.reset_syncing_to_failed().
"""
from __future__ import annotations

import logging
import os
import re
import shutil
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Optional

from noeta.agent.config import Settings
from noeta.agent.store.knowledge import KnowledgeSourceStore

logger = logging.getLogger(__name__)

# Sync threadpool size (number of sync jobs running concurrently)
_MAX_CONCURRENT_SYNCS = 4

# Redaction backstop before writing last_error: wipe `Bearer <token>` values
# and URL-embedded credentials so no secret leaks to space members through
# last_error (GET /spaces/{id}/knowledge returns the field verbatim).
_BEARER_RE = re.compile(r"Bearer\s+[A-Za-z0-9._\-]+")
_URL_CRED_RE = re.compile(r"://[^/@\s]+@")


def _redact(msg: str) -> str:
    """Wipe bearer tokens and URL credentials from an error message."""
    msg = _BEARER_RE.sub("Bearer ***", msg)
    return _URL_CRED_RE.sub("://***@", msg)


class KnowledgeSyncManager:
    """Knowledge-source sync orchestrator."""

    def __init__(
        self,
        store: KnowledgeSourceStore,
        settings: Settings,
        auth_provider: Any = None,
    ) -> None:
        self._store = store
        self._settings = settings
        # Auth seam kept for wiring compatibility: the built-in adapters do
        # not use it (git_repo authenticates with the token from its own
        # source config; local_dir needs no auth).
        self._auth_provider = auth_provider
        self._executor = ThreadPoolExecutor(
            max_workers=_MAX_CONCURRENT_SYNCS, thread_name_prefix="knowledge-sync"
        )
        # Tracks in-flight futures (source_id -> Future)
        self._running: dict[str, object] = {}
        # Sync progress (in-memory only, never persisted): source_id ->
        # progress dict. Losing it on backend restart is acceptable (status
        # is reset to failed anyway).
        self._progress: dict[str, dict] = {}
        self._progress_lock = threading.Lock()

    def start_sync(self, source_id: str, triggered_by: str) -> dict:
        """Trigger a sync. Returns the current source state.

        Raises:
            ValueError: the source does not exist or is already syncing
        """
        source = self._store.get_source(source_id)
        if source is None:
            raise ValueError("knowledge source not found")

        # Atomic claim: of concurrent POST sync requests only one can grab
        # syncing (closes the TOCTOU window)
        if not self._store.try_set_syncing(source_id):
            raise ValueError("this knowledge source is already syncing")

        future = self._executor.submit(
            self._run_sync, source_id, triggered_by
        )
        self._running[source_id] = future
        # Clean up when done
        future.add_done_callback(lambda f, sid=source_id: self._running.pop(sid, None))

        return self._store.get_source(source_id)  # type: ignore[return-value]

    def is_running(self, source_id: str) -> bool:
        f = self._running.get(source_id)
        return f is not None and not f.done()  # type: ignore[union-attr]

    def get_progress(self, source_id: str) -> Optional[dict]:
        """Read the sync progress (in-memory). Returns None when not syncing
        or no record exists."""
        with self._progress_lock:
            return self._progress.get(source_id)

    def _set_progress(self, source_id: str, progress: dict) -> None:
        with self._progress_lock:
            self._progress[source_id] = progress

    def _clear_progress(self, source_id: str) -> None:
        with self._progress_lock:
            self._progress.pop(source_id, None)

    def _run_sync(self, source_id: str, triggered_by: str) -> None:
        """Background thread: run the sync."""
        source = self._store.get_source(source_id)
        if source is None:
            return

        try:
            if source["type"] == "git_repo":
                self._sync_git_repo(source, triggered_by)
            elif source["type"] == "local_dir":
                self._sync_local_dir(source, triggered_by)
            else:
                raise ValueError(f"unknown source type: {source['type']}")

        except Exception as e:
            logger.exception("knowledge source sync failed: %s", source_id)
            # Redaction backstop: even if upstream let a secret slip into the
            # exception message, it must not reach last_error
            self._store.update_status(
                source_id, "failed",
                last_error=_redact(str(e))[:1000],
            )
        finally:
            # Clear the progress when the sync ends (success or failure)
            self._clear_progress(source_id)

    def _sync_git_repo(self, source: dict, triggered_by: str) -> None:
        """Sync a git repository (shallow clone / incremental fetch via the
        git CLI)."""
        from noeta.agent.services.knowledge.repo_sync import RepoSyncError, sync_repo

        config = source.get("config", {})
        url = config.get("url", "")
        branch = config.get("branch") or None
        token = config.get("token") or None

        if not url:
            raise ValueError("git_repo source is missing url")

        # Materialization directory
        out_dir = str(self._source_dir(source))

        # Never log the token; the URL in the config is the clean URL (the
        # credential is injected per git invocation inside repo_sync).
        logger.info(
            "starting repo sync: source=%s url=%s branch=%s out=%s",
            source["id"], url, branch or "(default)", out_dir,
        )

        # progress_callback: repo_sync's phase reports land in the in-memory
        # dict; GET sync status passes them through to the frontend
        # (consumed by a 2s poll).
        source_id = source["id"]
        self._set_progress(source_id, {"phase": "starting"})

        def on_progress(p: dict) -> None:
            self._set_progress(source_id, p)

        try:
            result = sync_repo(
                git_url=url,
                out_dir=out_dir,
                token=token,
                branch=branch,
                progress_callback=on_progress,
            )
        except RepoSyncError as e:
            raise RuntimeError(f"repository sync failed: {e}")

        # Maintain the name → id display symlink
        self._ensure_name_symlink(source)

        # Update the status
        self._store.update_status(
            source["id"], "ready",
            last_sync_at=time.time(),
            last_error=None,
        )
        logger.info(
            "repo sync finished: source=%s files=%d commit=%s",
            source["id"], result.get("file_count", 0), result.get("commit", "")[:12],
        )

    def _sync_local_dir(self, source: dict, triggered_by: str) -> None:
        """Sync a local directory: validate it, then copy its tree into the
        per-source knowledge directory.

        A copy, not a symlink: the materialized tree is bind-mounted
        read-only into sandboxes, so it must be self-contained — it keeps
        working if the original moves and never exposes anything outside the
        shared knowledge directory.
        """
        config = source.get("config", {})
        path = config.get("path", "")

        if not path:
            raise ValueError("local_dir source is missing path")
        src = Path(path)
        if not src.is_dir():
            raise ValueError(
                f"local_dir path does not exist or is not a directory: {path}"
            )

        # Materialization directory
        out_dir = self._source_dir(source)

        logger.info(
            "starting local-dir sync: source=%s path=%s out=%s",
            source["id"], src, out_dir,
        )

        source_id = source["id"]
        self._set_progress(source_id, {"phase": "copying"})

        # Full re-materialization: drop the previous copy so deletions in the
        # original propagate, then copy the tree (following symlinks so the
        # result is plain files; dangling links are skipped).
        if out_dir.is_dir():
            shutil.rmtree(out_dir)
        out_dir.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(
            src, out_dir, symlinks=False, ignore_dangling_symlinks=True
        )
        file_count = sum(len(files) for _root, _dirs, files in os.walk(out_dir))

        # Maintain the name → id display symlink
        self._ensure_name_symlink(source)

        # Update the status
        self._store.update_status(
            source["id"], "ready",
            last_sync_at=time.time(),
            last_error=None,
        )
        logger.info(
            "local-dir sync finished: source=%s files=%d",
            source["id"], file_count,
        )

    def _source_dir(self, source: dict) -> Path:
        """This source's materialization directory:
        shared_data_dir/knowledge/<space_id>/<id>/"""
        return (
            self._settings.knowledge_path
            / source["space_id"]
            / source["id"]
        )

    def _ensure_name_symlink(self, source: dict) -> None:
        """Maintain the name → id display symlink:
        knowledge/<space_id>/<source name> -> <id>/.

        Lets knowledge/<source name>/ written in SKILL.md resolve correctly
        to the id directory.
        """
        space_dir = self._settings.knowledge_path / source["space_id"]
        space_dir.mkdir(parents=True, exist_ok=True)

        name_link = space_dir / source["name"]
        target = Path(source["id"])

        # Skip when it already exists and points at the right place
        if name_link.is_symlink():
            try:
                if name_link.resolve().name == source["id"]:
                    return
            except OSError:
                pass
            name_link.unlink()
        elif name_link.exists():
            # A real file/directory with the same name; do not overwrite
            logger.warning(
                "name symlink target exists and is not a symlink, skipping: %s",
                name_link,
            )
            return

        try:
            name_link.symlink_to(target, target_is_directory=True)
        except OSError as e:
            logger.warning(
                "creating name symlink failed %s -> %s: %s", name_link, target, e
            )

    def rename_source_symlink(self, source: dict, old_name: str, new_name: str) -> None:
        """Maintain the name symlink when a knowledge source is renamed:
        remove the old name, then create the new one (if the source directory
        has been materialized).

        The source fields (space_id / id) are unchanged; they build the
        directory path, while name uses the old/new values passed in.
        """
        if old_name == new_name:
            return
        space_dir = self._settings.knowledge_path / source["space_id"]
        old_link = space_dir / old_name
        if old_link.is_symlink():
            old_link.unlink()
        # Only create the new symlink when the source directory is
        # materialized (a never-synced source has no id directory; creating
        # the link would leave it dangling)
        renamed = {**source, "name": new_name}
        if self._source_dir(renamed).is_dir():
            self._ensure_name_symlink(renamed)

    def delete_source_files(self, source: dict) -> None:
        """Delete the source's materialized directory + name symlink."""
        source_dir = self._source_dir(source)
        if source_dir.is_dir():
            shutil.rmtree(source_dir)

        space_dir = self._settings.knowledge_path / source["space_id"]
        name_link = space_dir / source["name"]
        if name_link.is_symlink():
            name_link.unlink()

    def shutdown(self) -> None:
        self._executor.shutdown(wait=False)
