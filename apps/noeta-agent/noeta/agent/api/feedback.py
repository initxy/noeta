"""Feedback-loop API: message-level thumbs up/down collection, attaching
references, analysis runs, and the suggestion surface.

Permission model (following the memories.py precedent):
- Submitting feedback / viewing the list / attaching a reference: all space
  members (the person supplying the finalized artifact is often the member
  who clicked the thumbs-down).
- Triggering analysis / adopting / dismissing suggestions: owner only —
  adoption directly changes the space's shared agent behavior assets
  (memories), matching the existing "space-skill writes are owner-only"
  boundary.
- Non-members -> 404 (hiding existence).

Adoption channels: memory = the owner edits the draft, which is then written
into space memory (new agent sessions recall it immediately); skill = a
suggestion carrying a skill_patch is **applied in one click after a backup**
to the space SKILL.md on adoption (without a patch it is only marked);
report suggestions are only marked. The report surface (phase 2): the owner
selects suggestions -> a report-mode run aggregates a markdown draft ->
after preview it is published as a markdown file.
"""
from __future__ import annotations

import time
from typing import Optional

import anyio
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from noeta.agent.auth.deps import CurrentUser, get_current_user
from noeta.agent.store.feedback import FeedbackStore
from noeta.agent.store.sessions import Session, SessionStore
from noeta.agent.store.spaces import ROLE_OWNER, SpaceStore

router = APIRouter(tags=["feedback"])

#: Preset tags for the thumbs-down popover (the frontend chips align with
#: these; "other" is carried by the free-text field).
VALID_TAGS = (
    "irrelevant answer",
    "incorrect result",
    "knowledge base not used",
    "wrong citation",
    "too slow",
    "other",
)


def _store(request: Request) -> FeedbackStore:
    return request.app.state.feedback_store


def _spaces(request: Request) -> SpaceStore:
    return request.app.state.space_store


def _sessions(request: Request) -> SessionStore:
    return request.app.state.session_store


def _membership_or_404(
    request: Request, space_id: str, user: CurrentUser
) -> tuple[dict, str]:
    space = _spaces(request).get_space(space_id)
    role = (
        _spaces(request).get_member_role(space_id, user.username) if space else None
    )
    if space is None or role is None:
        raise HTTPException(status_code=404, detail="space not found")
    return space, role


def _own_session(request: Request, session_id: str, user: CurrentUser) -> Session:
    session = _sessions(request).get(session_id)
    if session is None or not _spaces(request).is_member(
        session.space_id, user.username
    ):
        raise HTTPException(status_code=404, detail="session not found")
    return session


# ------------------------------------------------------------------ collection

class SubmitFeedbackBody(BaseModel):
    rating: int = Field(description="1=thumbs up / -1=thumbs down")
    task_id: Optional[str] = None
    event_seq: Optional[int] = None
    tags: list[str] = Field(default_factory=list)
    comment: str = Field(default="", max_length=4000)


@router.post("/sessions/{session_id}/feedback")
async def submit_feedback(
    session_id: str,
    body: SubmitFeedbackBody,
    request: Request,
    user: CurrentUser = Depends(get_current_user),
) -> dict:
    session = _own_session(request, session_id, user)
    if body.rating not in (1, -1):
        raise HTTPException(status_code=422, detail="rating must be 1 or -1")
    tags = [t for t in body.tags if t in VALID_TAGS]
    feedback = _store(request).create_feedback(
        space_id=session.space_id,
        session_id=session.id,
        task_id=body.task_id or session.task_id or "",
        event_seq=body.event_seq,
        author=user.username,
        rating=body.rating,
        tags=tags,
        comment=body.comment.strip(),
    )
    return {"feedback": feedback}


@router.get("/sessions/{session_id}/feedback")
async def list_session_feedback(
    session_id: str,
    request: Request,
    user: CurrentUser = Depends(get_current_user),
) -> dict:
    """Existing feedback for this session (the frontend marks which messages
    already have feedback)."""
    session = _own_session(request, session_id, user)
    items = _store(request).list_feedback(session.space_id, session_id=session.id)
    return {"feedback": items}


# ------------------------------------------------------------------ feedback page

@router.get("/spaces/{space_id}/feedback")
async def list_feedback(
    space_id: str,
    request: Request,
    user: CurrentUser = Depends(get_current_user),
) -> dict:
    _membership_or_404(request, space_id, user)
    store = _store(request)
    return {
        "feedback": store.list_feedback(space_id),
        "counts": store.counts(space_id),
        "tags": list(VALID_TAGS),
    }


class ReferenceBody(BaseModel):
    kind: str = Field(description="text")
    text: str = Field(default="", max_length=200_000)


@router.put("/spaces/{space_id}/feedback/{feedback_id}/reference")
async def put_reference(
    space_id: str,
    feedback_id: str,
    body: ReferenceBody,
    request: Request,
    user: CurrentUser = Depends(get_current_user),
) -> dict:
    """Attach a reference (the finalized artifact). It is materialized as a
    stable snapshot at submission time — failures error out on the spot (the
    background analysis has nobody to ask)."""
    _membership_or_404(request, space_id, user)
    store = _store(request)
    feedback = store.get_feedback(feedback_id)
    if feedback is None or feedback["space_id"] != space_id:
        raise HTTPException(status_code=404, detail="feedback not found")

    from noeta.agent.services.feedback_reference import (
        FeedbackReferenceService,
        ReferenceError,
    )

    service = FeedbackReferenceService(
        request.app.state.settings, request.app.state.auth_provider
    )
    try:
        if body.kind == "text":
            await anyio.to_thread.run_sync(
                service.materialize_text, space_id, feedback_id, body.text
            )
            updated = store.set_reference(feedback_id, "text", None)
        else:
            raise HTTPException(status_code=422, detail="kind must be text")
    except ReferenceError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"feedback": updated}


@router.get("/spaces/{space_id}/feedback/{feedback_id}/reference")
async def get_reference(
    space_id: str,
    feedback_id: str,
    request: Request,
    user: CurrentUser = Depends(get_current_user),
) -> dict:
    _membership_or_404(request, space_id, user)
    feedback = _store(request).get_feedback(feedback_id)
    if feedback is None or feedback["space_id"] != space_id:
        raise HTTPException(status_code=404, detail="feedback not found")
    from noeta.agent.services.feedback_reference import reference_path

    path = reference_path(request.app.state.settings, space_id, feedback_id)
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        raise HTTPException(status_code=404, detail="this feedback has no reference")
    return {"feedback_id": feedback_id, "text": text}


# ------------------------------------------------------------------ analysis runs

@router.post("/spaces/{space_id}/feedback/analyze")
async def trigger_analysis(
    space_id: str,
    request: Request,
    user: CurrentUser = Depends(get_current_user),
) -> dict:
    """The owner manually triggers one analysis run: batch attribution of all
    pending negative feedback (batching is what makes it worthwhile)."""
    space, role = _membership_or_404(request, space_id, user)
    if role != ROLE_OWNER:
        raise HTTPException(status_code=403, detail="space owner permission required")
    store = _store(request)
    if store.running_run(space_id) is not None:
        raise HTTPException(status_code=409, detail="an analysis is already running")
    pending = store.list_unanalyzed_negative(space_id)
    if not pending:
        raise HTTPException(
            status_code=400, detail="no negative feedback is pending analysis"
        )
    run = store.create_run(space_id, user.username)
    request.app.state.agent_service.start_feedback_analysis(
        space_id, space["name"], run["id"], pending
    )
    return {"run": run, "feedback_count": len(pending)}


@router.get("/spaces/{space_id}/feedback/runs/latest")
async def latest_run(
    space_id: str,
    request: Request,
    user: CurrentUser = Depends(get_current_user),
) -> dict:
    _membership_or_404(request, space_id, user)
    return {"run": _store(request).latest_run(space_id)}


# ------------------------------------------------------------------ suggestions

@router.get("/spaces/{space_id}/feedback/suggestions")
async def list_suggestions(
    space_id: str,
    request: Request,
    user: CurrentUser = Depends(get_current_user),
) -> dict:
    _membership_or_404(request, space_id, user)
    return {"suggestions": _store(request).list_suggestions(space_id)}


class AdoptBody(BaseModel):
    """Adoption payload for the memory channel: the owner-edited memory
    draft; the other channels leave it empty."""

    memory_name: str = Field(default="", max_length=200)
    memory_text: str = Field(default="", max_length=64_000)


def _suggestion_or_404(
    request: Request, space_id: str, suggestion_id: str
) -> dict:
    suggestion = _store(request).get_suggestion(suggestion_id)
    if suggestion is None or suggestion["space_id"] != space_id:
        raise HTTPException(status_code=404, detail="suggestion not found")
    return suggestion


@router.post("/spaces/{space_id}/feedback/suggestions/{suggestion_id}/adopt")
async def adopt_suggestion(
    space_id: str,
    suggestion_id: str,
    body: AdoptBody,
    request: Request,
    user: CurrentUser = Depends(get_current_user),
) -> dict:
    _, role = _membership_or_404(request, space_id, user)
    if role != ROLE_OWNER:
        raise HTTPException(status_code=403, detail="space owner permission required")
    suggestion = _suggestion_or_404(request, space_id, suggestion_id)
    if suggestion["status"] != "pending":
        raise HTTPException(
            status_code=409, detail="this suggestion has already been handled"
        )

    settings = request.app.state.settings
    adopted_result: dict = {}
    if suggestion["channel"] == "memory":
        name = body.memory_name.strip()
        text = body.memory_text.strip()
        if not name or not text:
            raise HTTPException(
                status_code=422,
                detail="adopting a memory suggestion requires memory_name and"
                " memory_text",
            )
        from noeta.tools.memory import MemoryStore

        try:
            MemoryStore(settings.memories_path / space_id).write(name, text)
        except ValueError:
            raise HTTPException(
                status_code=422, detail="memory names must be kebab-case slugs"
            )
        adopted_result = {"memory": name}
    elif suggestion["channel"] == "skill" and suggestion.get("skill_patch"):
        # One-click apply: back up the original SKILL.md, then overwrite the
        # whole file (space skills have no versioning; the backup is the only
        # rollback lever). Assembly is a per-skill symlink pointing at the
        # source directory, so the change only affects newly created sessions.
        name = (suggestion.get("skill_name") or "").strip()
        skill_file = settings.space_skills_path / space_id / name / "SKILL.md"
        if not name or not skill_file.is_file():
            raise HTTPException(
                status_code=409,
                detail=f"space skill {name or '?'} no longer exists; cannot apply"
                " the change",
            )
        backup_dir = settings.data_path / "feedback" / space_id / "skill-backups"
        backup_dir.mkdir(parents=True, exist_ok=True)
        backup = backup_dir / f"{name}-{int(time.time())}.md"
        backup.write_text(
            skill_file.read_text(encoding="utf-8"), encoding="utf-8"
        )
        skill_file.write_text(suggestion["skill_patch"], encoding="utf-8")
        adopted_result = {"skill": name, "backup": backup.name}

    updated = _store(request).decide_suggestion(
        suggestion_id, "adopted", user.username, adopted_result or None
    )
    if updated is None:
        raise HTTPException(
            status_code=409, detail="this suggestion has already been handled"
        )
    return {"suggestion": updated}


@router.get("/spaces/{space_id}/feedback/suggestions/{suggestion_id}/skill-diff")
async def skill_diff(
    space_id: str,
    suggestion_id: str,
    request: Request,
    user: CurrentUser = Depends(get_current_user),
) -> dict:
    """Diff material for a skill-channel suggestion: the current SKILL.md full
    text + the modified full text (the frontend renders the line-level diff).
    Members may preview (no side effects); applying still goes through the
    owner's adopt."""
    _membership_or_404(request, space_id, user)
    suggestion = _suggestion_or_404(request, space_id, suggestion_id)
    patch = suggestion.get("skill_patch")
    name = (suggestion.get("skill_name") or "").strip()
    if not patch or not name:
        raise HTTPException(
            status_code=404, detail="this suggestion has no applicable skill change"
        )
    settings = request.app.state.settings
    skill_file = settings.space_skills_path / space_id / name / "SKILL.md"
    try:
        current = skill_file.read_text(encoding="utf-8")
    except OSError:
        current = ""
    return {"skill_name": name, "current": current, "patched": patch}


@router.post("/spaces/{space_id}/feedback/suggestions/{suggestion_id}/dismiss")
async def dismiss_suggestion(
    space_id: str,
    suggestion_id: str,
    request: Request,
    user: CurrentUser = Depends(get_current_user),
) -> dict:
    _, role = _membership_or_404(request, space_id, user)
    if role != ROLE_OWNER:
        raise HTTPException(status_code=403, detail="space owner permission required")
    _suggestion_or_404(request, space_id, suggestion_id)
    updated = _store(request).decide_suggestion(
        suggestion_id, "dismissed", user.username
    )
    if updated is None:
        raise HTTPException(
            status_code=409, detail="this suggestion has already been handled"
        )
    return {"suggestion": updated}


# ------------------------------------------------------------------ reports

class GenerateReportBody(BaseModel):
    suggestion_ids: list[str] = Field(min_length=1)


@router.post("/spaces/{space_id}/feedback/report")
async def generate_report(
    space_id: str,
    body: GenerateReportBody,
    request: Request,
    user: CurrentUser = Depends(get_current_user),
) -> dict:
    """The owner selects suggestions -> a report-mode run aggregates them into
    a markdown draft (previewed on the platform; publishing is separate)."""
    space, role = _membership_or_404(request, space_id, user)
    if role != ROLE_OWNER:
        raise HTTPException(status_code=403, detail="space owner permission required")
    store = _store(request)
    if store.running_run(space_id) is not None:
        raise HTTPException(status_code=409, detail="an analysis is already running")
    suggestions = []
    for sid in dict.fromkeys(body.suggestion_ids):
        s = store.get_suggestion(sid)
        if s is None or s["space_id"] != space_id:
            raise HTTPException(status_code=404, detail=f"suggestion not found: {sid}")
        suggestions.append(s)
    # The feedback details referenced by the evidence are fed to the report
    # goal as well (for tools to look back at transcripts / references).
    feedback_map: dict[str, dict] = {}
    for s in suggestions:
        for ev in s.get("evidence") or []:
            fid = ev.get("feedback_id", "")
            if fid and fid not in feedback_map:
                fb = store.get_feedback(fid)
                if fb is not None:
                    feedback_map[fid] = fb
    run = store.create_run(space_id, user.username, kind="report")
    request.app.state.agent_service.start_feedback_report(
        space_id, space["name"], run["id"], user.username,
        suggestions, feedback_map,
    )
    return {"run": run}


@router.get("/spaces/{space_id}/feedback/reports")
async def list_reports(
    space_id: str,
    request: Request,
    user: CurrentUser = Depends(get_current_user),
) -> dict:
    _membership_or_404(request, space_id, user)
    return {"reports": _store(request).list_reports(space_id)}


@router.post("/spaces/{space_id}/feedback/reports/{report_id}/publish")
async def publish_report(
    space_id: str,
    report_id: str,
    request: Request,
    user: CurrentUser = Depends(get_current_user),
) -> dict:
    """Publish a draft report: the publishing service writes it out as a
    markdown file and the resulting path is recorded on the report."""
    _, role = _membership_or_404(request, space_id, user)
    if role != ROLE_OWNER:
        raise HTTPException(status_code=403, detail="space owner permission required")
    store = _store(request)
    report = store.get_report(report_id)
    if report is None or report["space_id"] != space_id:
        raise HTTPException(status_code=404, detail="report not found")
    if report["status"] != "draft":
        raise HTTPException(status_code=409, detail="the report is already published")

    from noeta.agent.services.feedback_report import (
        ReportPublishError,
        publish_report_to_file,
    )

    try:
        path = await anyio.to_thread.run_sync(
            publish_report_to_file,
            request.app.state.settings,
            request.app.state.auth_provider,
            user.username,
            report["title"],
            report["body"],
        )
    except ReportPublishError as e:
        raise HTTPException(status_code=400, detail=str(e))
    updated = store.publish_report(report_id, path)
    if updated is None:
        raise HTTPException(status_code=409, detail="the report is already published")
    return {"report": updated}
