"""Feedback-loop store (sqlite3, WAL, thread-safe within the process).

Four tables:
- feedback: message-level thumbs up/down (anchor = session_id + task_id +
  event_seq) + an optional reference. The reference snapshot body goes to a
  file (DATA_DIR/feedback/<space_id>/<feedback_id>/reference.md — the path
  is derived by convention and never stored); the table keeps only kind /
  origin_url.
- feedback_suggestions: structured improvement suggestions produced by the
  analysis agent (evidence is required). The skill channel may carry a
  skill_patch (the full modified SKILL.md, space skills only); on adoption
  it is applied in one click after a backup (phase 2).
- feedback_analysis_runs: the lifecycle of one run (kind = analysis
  attribution / report summarization; at most one running per space at a
  time — both kinds share that constraint).
- feedback_reports: the artifact of a report-mode run (markdown draft → the
  document URL is backfilled into doc_url after publishing).

Incremental-analysis semantics: when a run consumes feedback it backfills
feedback.analyzed_run_id; adding a reference to feedback that was **already
analyzed** resets analyzed_run_id back to NULL — a reference is the
strongest attribution evidence and is worth re-attributing next round.
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid
from pathlib import Path
from typing import Optional

_SCHEMA = """
CREATE TABLE IF NOT EXISTS feedback (
    id              TEXT PRIMARY KEY,
    space_id        TEXT NOT NULL,
    session_id      TEXT NOT NULL,
    task_id         TEXT NOT NULL DEFAULT '',
    event_seq       INTEGER,
    author          TEXT NOT NULL,
    rating          INTEGER NOT NULL,
    tags            TEXT NOT NULL DEFAULT '[]',
    comment         TEXT NOT NULL DEFAULT '',
    reference_kind  TEXT NOT NULL DEFAULT 'none',
    reference_origin_url TEXT,
    analyzed_run_id TEXT,
    created_at      REAL NOT NULL,
    updated_at      REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_feedback_space
    ON feedback(space_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_feedback_session
    ON feedback(session_id);

CREATE TABLE IF NOT EXISTS feedback_suggestions (
    id          TEXT PRIMARY KEY,
    space_id    TEXT NOT NULL,
    run_id      TEXT NOT NULL,
    channel     TEXT NOT NULL,
    title       TEXT NOT NULL,
    body        TEXT NOT NULL,
    skill_name  TEXT,
    skill_patch TEXT,
    evidence    TEXT NOT NULL DEFAULT '[]',
    status      TEXT NOT NULL DEFAULT 'pending',
    adopted_result TEXT,
    created_at  REAL NOT NULL,
    decided_at  REAL,
    decided_by  TEXT
);
CREATE INDEX IF NOT EXISTS idx_suggestions_space
    ON feedback_suggestions(space_id, created_at DESC);

CREATE TABLE IF NOT EXISTS feedback_analysis_runs (
    id           TEXT PRIMARY KEY,
    space_id     TEXT NOT NULL,
    kind         TEXT NOT NULL DEFAULT 'analysis',
    status       TEXT NOT NULL DEFAULT 'running',
    triggered_by TEXT NOT NULL,
    task_id      TEXT,
    error        TEXT,
    started_at   REAL NOT NULL,
    finished_at  REAL
);
CREATE INDEX IF NOT EXISTS idx_runs_space
    ON feedback_analysis_runs(space_id, started_at DESC);

CREATE TABLE IF NOT EXISTS feedback_reports (
    id           TEXT PRIMARY KEY,
    space_id     TEXT NOT NULL,
    run_id       TEXT NOT NULL,
    title        TEXT NOT NULL,
    body         TEXT NOT NULL,
    status       TEXT NOT NULL DEFAULT 'draft',
    doc_url      TEXT,
    created_by   TEXT NOT NULL,
    created_at   REAL NOT NULL,
    published_at REAL
);
CREATE INDEX IF NOT EXISTS idx_reports_space
    ON feedback_reports(space_id, created_at DESC);
"""

VALID_CHANNELS = ("memory", "skill", "report")
VALID_SUGGESTION_STATUSES = ("pending", "adopted", "dismissed")
VALID_RUN_STATUSES = ("running", "done", "failed")
VALID_REFERENCE_KINDS = ("none", "text", "doc")

_FEEDBACK_COLS = (
    "id,space_id,session_id,task_id,event_seq,author,rating,tags,comment,"
    "reference_kind,reference_origin_url,analyzed_run_id,created_at,updated_at"
)
_SUGGESTION_COLS = (
    "id,space_id,run_id,channel,title,body,skill_name,skill_patch,evidence,"
    "status,adopted_result,created_at,decided_at,decided_by"
)
_RUN_COLS = (
    "id,space_id,kind,status,triggered_by,task_id,error,started_at,finished_at"
)
_REPORT_COLS = (
    "id,space_id,run_id,title,body,status,doc_url,created_by,created_at,"
    "published_at"
)


def _loads(text: Optional[str], fallback):
    try:
        return json.loads(text) if text else fallback
    except (json.JSONDecodeError, TypeError):
        return fallback


def _row_to_feedback(row: tuple) -> dict:
    return {
        "id": row[0],
        "space_id": row[1],
        "session_id": row[2],
        "task_id": row[3],
        "event_seq": row[4],
        "author": row[5],
        "rating": row[6],
        "tags": _loads(row[7], []),
        "comment": row[8],
        "reference_kind": row[9],
        "reference_origin_url": row[10],
        "analyzed_run_id": row[11],
        "created_at": row[12],
        "updated_at": row[13],
    }


def _row_to_suggestion(row: tuple) -> dict:
    return {
        "id": row[0],
        "space_id": row[1],
        "run_id": row[2],
        "channel": row[3],
        "title": row[4],
        "body": row[5],
        "skill_name": row[6],
        "skill_patch": row[7],
        "evidence": _loads(row[8], []),
        "status": row[9],
        "adopted_result": _loads(row[10], None),
        "created_at": row[11],
        "decided_at": row[12],
        "decided_by": row[13],
    }


def _row_to_run(row: tuple) -> dict:
    return {
        "id": row[0],
        "space_id": row[1],
        "kind": row[2],
        "status": row[3],
        "triggered_by": row[4],
        "task_id": row[5],
        "error": row[6],
        "started_at": row[7],
        "finished_at": row[8],
    }


def _row_to_report(row: tuple) -> dict:
    return {
        "id": row[0],
        "space_id": row[1],
        "run_id": row[2],
        "title": row[3],
        "body": row[4],
        "status": row[5],
        "doc_url": row[6],
        "created_by": row[7],
        "created_at": row[8],
        "published_at": row[9],
    }


class FeedbackStore:
    def __init__(self, db_path: Path) -> None:
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(
            str(db_path), check_same_thread=False, isolation_level=None
        )
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA busy_timeout=5000")
        self._conn.executescript(_SCHEMA)
        self._ensure_phase2_columns()
        self._lock = threading.Lock()

    def _ensure_phase2_columns(self) -> None:
        """Phase-1 legacy-database migration: add skill_patch to suggestions
        and kind to runs (skipped if they already exist)."""
        cols = {
            r[1]
            for r in self._conn.execute(
                "PRAGMA table_info(feedback_suggestions)"
            ).fetchall()
        }
        if "skill_patch" not in cols:
            self._conn.execute(
                "ALTER TABLE feedback_suggestions ADD COLUMN skill_patch TEXT"
            )
        cols = {
            r[1]
            for r in self._conn.execute(
                "PRAGMA table_info(feedback_analysis_runs)"
            ).fetchall()
        }
        if "kind" not in cols:
            self._conn.execute(
                "ALTER TABLE feedback_analysis_runs ADD COLUMN kind TEXT"
                " NOT NULL DEFAULT 'analysis'"
            )

    def close(self) -> None:
        self._conn.close()

    # ------------------------------------------------------------ feedback

    def create_feedback(
        self,
        space_id: str,
        session_id: str,
        task_id: str,
        event_seq: Optional[int],
        author: str,
        rating: int,
        tags: list[str],
        comment: str,
    ) -> dict:
        if rating not in (1, -1):
            raise ValueError("rating must be 1 or -1")
        fid = uuid.uuid4().hex
        now = time.time()
        with self._lock:
            self._conn.execute(
                f"INSERT INTO feedback ({_FEEDBACK_COLS})"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    fid, space_id, session_id, task_id, event_seq, author,
                    rating, json.dumps(tags, ensure_ascii=False), comment,
                    "none", None, None, now, now,
                ),
            )
        return self.get_feedback(fid)  # type: ignore[return-value]

    def get_feedback(self, feedback_id: str) -> Optional[dict]:
        with self._lock:
            row = self._conn.execute(
                f"SELECT {_FEEDBACK_COLS} FROM feedback WHERE id=?",
                (feedback_id,),
            ).fetchone()
        return _row_to_feedback(row) if row else None

    def list_feedback(
        self, space_id: str, session_id: Optional[str] = None
    ) -> list[dict]:
        sql = f"SELECT {_FEEDBACK_COLS} FROM feedback WHERE space_id=?"
        args: list = [space_id]
        if session_id:
            sql += " AND session_id=?"
            args.append(session_id)
        sql += " ORDER BY created_at DESC"
        with self._lock:
            rows = self._conn.execute(sql, args).fetchall()
        return [_row_to_feedback(r) for r in rows]

    def counts(self, space_id: str) -> dict:
        """Positive/negative feedback counts for the space (thumbs-up is
        only counted in phase 1, it does not enter analysis)."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT rating, COUNT(*) FROM feedback WHERE space_id=?"
                " GROUP BY rating",
                (space_id,),
            ).fetchall()
        by_rating = {r[0]: r[1] for r in rows}
        return {"positive": by_rating.get(1, 0), "negative": by_rating.get(-1, 0)}

    def set_reference(
        self, feedback_id: str, kind: str, origin_url: Optional[str]
    ) -> Optional[dict]:
        """Record the reference metadata (the snapshot file is written by
        the caller).

        Adding a reference to already-analyzed feedback → analyzed_run_id is
        set to NULL (re-attributed in the next round).
        """
        if kind not in VALID_REFERENCE_KINDS:
            raise ValueError(f"Invalid reference kind: {kind}")
        with self._lock:
            cur = self._conn.execute(
                "UPDATE feedback SET reference_kind=?, reference_origin_url=?,"
                " analyzed_run_id=NULL, updated_at=? WHERE id=?",
                (kind, origin_url, time.time(), feedback_id),
            )
            if cur.rowcount == 0:
                return None
        return self.get_feedback(feedback_id)

    def list_unanalyzed_negative(self, space_id: str) -> list[dict]:
        """Negative feedback pending analysis: never-analyzed new feedback ∪
        old feedback that gained a reference after being analyzed."""
        with self._lock:
            rows = self._conn.execute(
                f"SELECT {_FEEDBACK_COLS} FROM feedback"
                " WHERE space_id=? AND rating=-1 AND analyzed_run_id IS NULL"
                " ORDER BY created_at ASC",
                (space_id,),
            ).fetchall()
        return [_row_to_feedback(r) for r in rows]

    def mark_analyzed(self, feedback_ids: list[str], run_id: str) -> None:
        if not feedback_ids:
            return
        with self._lock:
            self._conn.executemany(
                "UPDATE feedback SET analyzed_run_id=?, updated_at=? WHERE id=?",
                [(run_id, time.time(), fid) for fid in feedback_ids],
            )

    # ------------------------------------------------------------ runs

    def create_run(
        self, space_id: str, triggered_by: str, kind: str = "analysis"
    ) -> dict:
        if kind not in ("analysis", "report"):
            raise ValueError(f"Invalid run kind: {kind}")
        rid = uuid.uuid4().hex
        with self._lock:
            self._conn.execute(
                f"INSERT INTO feedback_analysis_runs ({_RUN_COLS})"
                " VALUES (?,?,?,?,?,?,?,?,?)",
                (rid, space_id, kind, "running", triggered_by, None, None,
                 time.time(), None),
            )
        return self.get_run(rid)  # type: ignore[return-value]

    def get_run(self, run_id: str) -> Optional[dict]:
        with self._lock:
            row = self._conn.execute(
                f"SELECT {_RUN_COLS} FROM feedback_analysis_runs WHERE id=?",
                (run_id,),
            ).fetchone()
        return _row_to_run(row) if row else None

    def latest_run(self, space_id: str) -> Optional[dict]:
        with self._lock:
            row = self._conn.execute(
                f"SELECT {_RUN_COLS} FROM feedback_analysis_runs"
                " WHERE space_id=? ORDER BY started_at DESC LIMIT 1",
                (space_id,),
            ).fetchone()
        return _row_to_run(row) if row else None

    def running_run(self, space_id: str) -> Optional[dict]:
        with self._lock:
            row = self._conn.execute(
                f"SELECT {_RUN_COLS} FROM feedback_analysis_runs"
                " WHERE space_id=? AND status='running'"
                " ORDER BY started_at DESC LIMIT 1",
                (space_id,),
            ).fetchone()
        return _row_to_run(row) if row else None

    def set_run_task(self, run_id: str, task_id: str) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE feedback_analysis_runs SET task_id=? WHERE id=?",
                (task_id, run_id),
            )

    def finish_run(
        self, run_id: str, status: str, error: Optional[str] = None
    ) -> None:
        if status not in ("done", "failed"):
            raise ValueError(f"Invalid terminal run status: {status}")
        with self._lock:
            self._conn.execute(
                "UPDATE feedback_analysis_runs SET status=?, error=?,"
                " finished_at=? WHERE id=?",
                (status, error, time.time(), run_id),
            )

    def reset_stale_running(self) -> None:
        """Process restart: stale running rows are set to failed (nobody is
        left to finish the seeded analysis task's events)."""
        with self._lock:
            self._conn.execute(
                "UPDATE feedback_analysis_runs SET status='failed',"
                " error='Backend restarted; analysis interrupted', finished_at=?"
                " WHERE status='running'",
                (time.time(),),
            )

    # ------------------------------------------------------------ suggestions

    def create_suggestion(
        self,
        space_id: str,
        run_id: str,
        channel: str,
        title: str,
        body: str,
        evidence: list,
        skill_name: Optional[str] = None,
        skill_patch: Optional[str] = None,
    ) -> dict:
        if channel not in VALID_CHANNELS:
            raise ValueError(f"Invalid suggestion channel: {channel}")
        if not title.strip() or not body.strip():
            raise ValueError("suggestion title / body must not be empty")
        if not evidence:
            raise ValueError("a suggestion must include evidence")
        if skill_patch and channel != "skill":
            raise ValueError("skill_patch is only allowed on the skill channel")
        sid = uuid.uuid4().hex
        with self._lock:
            self._conn.execute(
                f"INSERT INTO feedback_suggestions ({_SUGGESTION_COLS})"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    sid, space_id, run_id, channel, title.strip(), body,
                    skill_name, skill_patch,
                    json.dumps(evidence, ensure_ascii=False),
                    "pending", None, time.time(), None, None,
                ),
            )
        return self.get_suggestion(sid)  # type: ignore[return-value]

    def get_suggestion(self, suggestion_id: str) -> Optional[dict]:
        with self._lock:
            row = self._conn.execute(
                f"SELECT {_SUGGESTION_COLS} FROM feedback_suggestions WHERE id=?",
                (suggestion_id,),
            ).fetchone()
        return _row_to_suggestion(row) if row else None

    def list_suggestions(self, space_id: str) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                f"SELECT {_SUGGESTION_COLS} FROM feedback_suggestions"
                " WHERE space_id=? ORDER BY created_at DESC",
                (space_id,),
            ).fetchall()
        return [_row_to_suggestion(r) for r in rows]

    # ------------------------------------------------------------ reports

    def create_report(
        self,
        space_id: str,
        run_id: str,
        title: str,
        body: str,
        created_by: str,
    ) -> dict:
        if not title.strip() or not body.strip():
            raise ValueError("report title / body must not be empty")
        rid = uuid.uuid4().hex
        with self._lock:
            self._conn.execute(
                f"INSERT INTO feedback_reports ({_REPORT_COLS})"
                " VALUES (?,?,?,?,?,?,?,?,?,?)",
                (
                    rid, space_id, run_id, title.strip(), body, "draft",
                    None, created_by, time.time(), None,
                ),
            )
        return self.get_report(rid)  # type: ignore[return-value]

    def get_report(self, report_id: str) -> Optional[dict]:
        with self._lock:
            row = self._conn.execute(
                f"SELECT {_REPORT_COLS} FROM feedback_reports WHERE id=?",
                (report_id,),
            ).fetchone()
        return _row_to_report(row) if row else None

    def list_reports(self, space_id: str) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                f"SELECT {_REPORT_COLS} FROM feedback_reports"
                " WHERE space_id=? ORDER BY created_at DESC",
                (space_id,),
            ).fetchall()
        return [_row_to_report(r) for r in rows]

    def publish_report(self, report_id: str, doc_url: str) -> Optional[dict]:
        with self._lock:
            cur = self._conn.execute(
                "UPDATE feedback_reports SET status='published', doc_url=?,"
                " published_at=? WHERE id=? AND status='draft'",
                (doc_url, time.time(), report_id),
            )
            if cur.rowcount == 0:
                return None
        return self.get_report(report_id)

    def decide_suggestion(
        self,
        suggestion_id: str,
        status: str,
        decided_by: str,
        adopted_result: Optional[dict] = None,
    ) -> Optional[dict]:
        """Adopt / dismiss (only pending rows can be decided; on an
        idempotency conflict returns None, which the API layer reports as
        409)."""
        if status not in ("adopted", "dismissed"):
            raise ValueError(f"Invalid terminal suggestion status: {status}")
        with self._lock:
            cur = self._conn.execute(
                "UPDATE feedback_suggestions SET status=?, adopted_result=?,"
                " decided_at=?, decided_by=? WHERE id=? AND status='pending'",
                (
                    status,
                    json.dumps(adopted_result, ensure_ascii=False)
                    if adopted_result is not None
                    else None,
                    time.time(), decided_by, suggestion_id,
                ),
            )
            if cur.rowcount == 0:
                return None
        return self.get_suggestion(suggestion_id)
