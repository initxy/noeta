"""Task-board endpoints: card CRUD / column moves / starting an execution
session from a template.

One board per space (three fixed columns); disabled in personal spaces.
Multi-user edits get no real-time push (frontend refresh / polling is enough;
the acceptance criteria explicitly do not require it).
"""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from noeta.agent.api.spaces import require_space_member
from noeta.agent.auth.deps import CurrentUser, get_current_user
from noeta.agent.store.board import BOARD_COLUMNS
from noeta.agent.store.spaces import ROLE_OWNER

router = APIRouter(tags=["board"])


def _board(request: Request):
    return request.app.state.board_store


def _spaces(request: Request):
    return request.app.state.space_store


def _card_or_404(request: Request, card_id: str, user: CurrentUser) -> dict:
    card = _board(request).get_card(card_id)
    if card is None or not _spaces(request).is_member(
        card["space_id"], user.username
    ):
        raise HTTPException(status_code=404, detail="card not found")
    return card


class CreateCardBody(BaseModel):
    title: str = Field(min_length=1, max_length=100)
    description: str = Field(default="", max_length=4000)
    column_key: str = Field(default="todo")
    assignee: Optional[str] = None
    due_date: Optional[str] = Field(default=None, max_length=10)
    links: list[dict] = Field(default_factory=list)


class UpdateCardBody(BaseModel):
    title: Optional[str] = Field(default=None, min_length=1, max_length=100)
    description: Optional[str] = Field(default=None, max_length=4000)
    column_key: Optional[str] = None
    position: Optional[float] = None
    assignee: Optional[str] = None
    due_date: Optional[str] = Field(default=None, max_length=10)
    # Passing "" clears assignee / due_date (None = leave unchanged).
    clear_assignee: bool = False
    clear_due_date: bool = False


class StartSessionBody(BaseModel):
    template_id: str = Field(min_length=1)
    params: dict = Field(default_factory=dict)


@router.get("/spaces/{space_id}/board")
async def list_cards(
    space_id: str, request: Request, user: CurrentUser = Depends(get_current_user)
) -> dict:
    require_space_member(request, space_id, user)
    return {"cards": _board(request).list_cards(space_id), "columns": BOARD_COLUMNS}


@router.post("/spaces/{space_id}/board/cards", status_code=201)
async def create_card(
    space_id: str,
    body: CreateCardBody,
    request: Request,
    user: CurrentUser = Depends(get_current_user),
) -> dict:
    require_space_member(request, space_id, user)
    space = _spaces(request).get_space(space_id)
    if space and space.get("is_personal"):
        raise HTTPException(
            status_code=422, detail="personal spaces do not support the board"
        )
    try:
        card = _board(request).create_card(
            space_id,
            body.title.strip(),
            created_by=user.username,
            column_key=body.column_key,
            description=body.description,
            assignee=body.assignee or None,
            due_date=body.due_date or None,
            links=body.links,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    return {"card": card}


@router.patch("/board/cards/{card_id}")
async def update_card(
    card_id: str,
    body: UpdateCardBody,
    request: Request,
    user: CurrentUser = Depends(get_current_user),
) -> dict:
    _card_or_404(request, card_id, user)
    fields: dict = {}
    if body.title is not None:
        fields["title"] = body.title.strip()
    if body.description is not None:
        fields["description"] = body.description
    if body.column_key is not None:
        fields["column_key"] = body.column_key
    if body.position is not None:
        fields["position"] = body.position
    if body.assignee is not None or body.clear_assignee:
        fields["assignee"] = None if body.clear_assignee else body.assignee
    if body.due_date is not None or body.clear_due_date:
        fields["due_date"] = None if body.clear_due_date else body.due_date
    try:
        if fields:
            _board(request).update_card(card_id, **fields)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    return {"card": _board(request).get_card(card_id)}


@router.delete("/board/cards/{card_id}")
async def delete_card(
    card_id: str, request: Request, user: CurrentUser = Depends(get_current_user)
) -> dict:
    card = _card_or_404(request, card_id, user)
    role = _spaces(request).get_member_role(card["space_id"], user.username)
    if card["created_by"] != user.username and role != ROLE_OWNER:
        raise HTTPException(
            status_code=403,
            detail="only the card creator or a space owner can delete a card",
        )
    _board(request).delete_card(card_id)
    return {"ok": True}


@router.post("/board/cards/{card_id}/start-session", status_code=201)
async def start_card_session(
    card_id: str,
    body: StartSessionBody,
    request: Request,
    user: CurrentUser = Depends(get_current_user),
) -> dict:
    """Pick a template on the card and start an execution session (a normal
    session owned by the initiator); the session link is written back onto the
    card automatically."""
    from noeta.agent.models_config import get_default_model, validate_model
    from noeta.agent.workflow.templates import render_prompt

    card = _card_or_404(request, card_id, user)
    space_id = card["space_id"]
    templates = request.app.state.template_store
    tpl = templates.get_template(space_id, body.template_id)
    if tpl is None:
        raise HTTPException(status_code=422, detail="template not found")
    missing = [
        p["name"] for p in tpl["params"]
        if p.get("required") and not str(body.params.get(p["name"]) or "").strip()
    ]
    if missing:
        raise HTTPException(
            status_code=422, detail=f"missing required params: {', '.join(missing)}"
        )

    settings = request.app.state.settings
    model = request.app.state.agent_config_store.get(space_id).get("default_model")
    if model:
        try:
            validate_model(settings, model)
        except ValueError:
            model = None
    model = model or get_default_model(settings).id

    store = request.app.state.session_store
    session = store.create(user.username, model, space_id, template_id=tpl["id"])
    store.update(session.id, title=card["title"][:40], title_generated=1)
    session = store.get(session.id) or session
    request.app.state.agent_service.send_message(
        session, render_prompt(tpl["prompt"], body.params), model
    )
    card = request.app.state.board_store.add_link(
        card_id,
        {"type": "session", "id": session.id, "label": card["title"][:40]},
    )
    return {"card": card, "session": session.to_api()}
