"""Feedback improvement-report publishing: markdown draft → markdown file.

The publish action is triggered by the owner. This port replaces the
original "publish to a hosted document" step with writing the report as a
markdown file under ``settings.data_path / "reports"``: report generation is
unchanged, only the publish target moved. The returned value is the written
file's path — it fills the slot that used to carry the hosted-document URL
(the store records it, the frontend links to it).
"""
from __future__ import annotations

import logging
import re
import time
import uuid

from noeta.agent.config import Settings

logger = logging.getLogger(__name__)

#: Filename slug cap: keep report filenames short and filesystem-safe.
_SLUG_MAX_LEN = 60


class ReportPublishError(Exception):
    """Publishing failed (user-readable message; the API layer passes it
    through as the 4xx detail)."""


def _safe_filename(title: str) -> str:
    """Reduce a report title to a filesystem-safe slug (whitelist:
    alphanumerics, dash, underscore; everything else collapses to ``_``)."""
    slug = re.sub(r"[^A-Za-z0-9._-]+", "_", title).strip("._-")
    return slug[:_SLUG_MAX_LEN] or "report"


def publish_report_to_file(
    settings: Settings,
    auth_provider,
    username: str,
    title: str,
    body: str,
) -> str:
    """Write the report markdown to a file and return its path.

    The file lands under ``settings.data_path / "reports"`` (created on
    demand) as ``<timestamp>-<title slug>-<short id>.md``, with the title as
    an H1 heading followed by the body (the hosted document used to carry the
    title; the file embeds it instead). ``auth_provider`` is accepted for
    wiring compatibility and unused — writing a local file needs no external
    identity.

    Blocking IO (file write); callers run it via a thread pool. Failures
    raise ReportPublishError.
    """
    reports_dir = settings.data_path / "reports"
    stamp = time.strftime("%Y%m%d-%H%M%S")
    filename = f"{stamp}-{_safe_filename(title)}-{uuid.uuid4().hex[:8]}.md"
    path = reports_dir / filename
    content = f"# {title}\n\n{body}\n"
    try:
        reports_dir.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    except OSError as e:
        logger.warning("report publish failed: user=%s err=%s", username, e)
        raise ReportPublishError(f"failed to write the report file: {str(e)[:200]}")

    logger.info("report published: user=%s path=%s", username, path)
    return str(path)
