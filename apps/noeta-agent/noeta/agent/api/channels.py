"""Channel endpoints: channel CRUD, message history, the SSE stream
(since_seq replay + in-process hub increments), posting messages (@Agent
starts a topic), in-topic follow-ups / answers, and the unread watermark.

The topic view (right-hand panel) is not here — the full conversation stream
reuses the existing session SSE ``GET /sessions/{id}/events?task_id=``
(channel-session visibility = space members, the same semantics as
_own_session).
"""
from __future__ import annotations

import asyncio
import json
from typing import AsyncIterator, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from noeta.agent.api.spaces import require_space_member
from noeta.agent.auth.deps import CurrentUser, get_current_user
from noeta.agent.services.channels import ChannelService, TopicBusyError
from noeta.agent.store.spaces import ROLE_OWNER

router = APIRouter(tags=["channels"])

_HEARTBEAT_SECONDS = 15.0
_REPLAY_BATCH = 500


def _channels(request: Request):
    return request.app.state.channel_store


def _channel_service(request: Request) -> ChannelService:
    return request.app.state.channel_service


def _spaces(request: Request):
    return request.app.state.space_store


def _channel_or_404(request: Request, channel_id: str, user: CurrentUser) -> dict:
    """Channel visibility = membership of the owning space; otherwise 404
    (hiding existence)."""
    channel = _channels(request).get_channel(channel_id)
    if channel is None or not _spaces(request).is_member(
        channel["space_id"], user.username
    ):
        raise HTTPException(status_code=404, detail="channel not found")
    return channel


def _require_owner(request: Request, space_id: str, user: CurrentUser) -> None:
    if _spaces(request).get_member_role(space_id, user.username) != ROLE_OWNER:
        raise HTTPException(
            status_code=403, detail="only the space owner can do this"
        )


# ------------------------------------------------------------------- CRUD
class CreateChannelBody(BaseModel):
    name: str = Field(min_length=1, max_length=60)
    description: str = Field(default="", max_length=500)


class UpdateChannelBody(BaseModel):
    name: Optional[str] = Field(default=None, min_length=1, max_length=60)
    description: Optional[str] = Field(default=None, max_length=500)
    archived: Optional[bool] = None


@router.get("/spaces/{space_id}/channels")
async def list_channels(
    space_id: str, request: Request, user: CurrentUser = Depends(get_current_user)
) -> dict:
    require_space_member(request, space_id, user)
    store = _channels(request)
    unread = store.unread_counts(space_id, user.username)
    return {
        "channels": [
            {**c, "unread": unread.get(c["id"], 0)}
            for c in store.list_channels(space_id)
        ]
    }


@router.post("/spaces/{space_id}/channels", status_code=201)
async def create_channel(
    space_id: str,
    body: CreateChannelBody,
    request: Request,
    user: CurrentUser = Depends(get_current_user),
) -> dict:
    require_space_member(request, space_id, user)
    space = _spaces(request).get_space(space_id)
    if space and space.get("is_personal"):
        raise HTTPException(
            status_code=422, detail="personal spaces do not support channels"
        )
    channel = _channels(request).create_channel(
        space_id, body.name.strip(), user.username, body.description.strip()
    )
    return {"channel": {**channel, "unread": 0}}


@router.patch("/channels/{channel_id}")
async def update_channel(
    channel_id: str,
    body: UpdateChannelBody,
    request: Request,
    user: CurrentUser = Depends(get_current_user),
) -> dict:
    channel = _channel_or_404(request, channel_id, user)
    _require_owner(request, channel["space_id"], user)
    fields: dict = {}
    if body.name is not None:
        fields["name"] = body.name.strip()
    if body.description is not None:
        fields["description"] = body.description.strip()
    if body.archived is not None:
        fields["archived"] = 1 if body.archived else 0
    if fields:
        _channels(request).update_channel(channel_id, **fields)
    return {"channel": _channels(request).get_channel(channel_id)}


# ---------------------------------------------------------------- messages and topics
class PostMessageBody(BaseModel):
    text: str = Field(min_length=1, max_length=8000)
    # @Agent comes explicitly from the frontend mention interaction (the
    # backend does no body-text parsing).
    mention_agent: bool = False


class TopicMessageBody(BaseModel):
    text: str = Field(min_length=1, max_length=32000)


class TopicAnswerBody(BaseModel):
    question_id: str = Field(min_length=1)
    answers: dict = Field(default_factory=dict)


class ReadBody(BaseModel):
    seq: int = Field(ge=0)


@router.get("/channels/{channel_id}/messages")
async def list_messages(
    channel_id: str,
    request: Request,
    before_seq: Optional[int] = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    user: CurrentUser = Depends(get_current_user),
) -> dict:
    """History pagination (one ascending page) + the topic snapshot (for
    rendering topic cards on first paint)."""
    channel = _channel_or_404(request, channel_id, user)
    messages = _channels(request).list_messages(
        channel_id, before_seq=before_seq, limit=limit
    )
    return {
        "messages": messages,
        "topics": _channel_service(request).topic_views(channel),
    }


@router.post("/channels/{channel_id}/messages", status_code=202)
async def post_message(
    channel_id: str,
    body: PostMessageBody,
    request: Request,
    user: CurrentUser = Depends(get_current_user),
) -> dict:
    channel = _channel_or_404(request, channel_id, user)
    if channel["archived"]:
        raise HTTPException(status_code=409, detail="the channel is archived")
    try:
        message, topic = _channel_service(request).post_message(
            channel, user.username, body.text, body.mention_agent
        )
    except TopicBusyError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    # One's own message counts as read: push the watermark to it so the unread
    # badge never grows from one's own posts.
    _channels(request).set_read(channel_id, user.username, message["seq"])
    return {"message": message, "topic": topic}


@router.post("/channels/{channel_id}/topics/{topic_id}/messages", status_code=202)
async def topic_message(
    channel_id: str,
    topic_id: str,
    body: TopicMessageBody,
    request: Request,
    user: CurrentUser = Depends(get_current_user),
) -> dict:
    channel = _channel_or_404(request, channel_id, user)
    topic = _channels(request).get_topic(topic_id)
    if topic is None or topic["channel_id"] != channel_id:
        raise HTTPException(status_code=404, detail="topic not found")
    try:
        _channel_service(request).topic_message(channel, topic, user.username, body.text)
    except TopicBusyError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    return {"status": "started"}


@router.post("/channels/{channel_id}/topics/{topic_id}/answer", status_code=202)
async def topic_answer(
    channel_id: str,
    topic_id: str,
    body: TopicAnswerBody,
    request: Request,
    user: CurrentUser = Depends(get_current_user),
) -> dict:
    channel = _channel_or_404(request, channel_id, user)
    topic = _channels(request).get_topic(topic_id)
    if topic is None or topic["channel_id"] != channel_id:
        raise HTTPException(status_code=404, detail="topic not found")
    try:
        _channel_service(request).topic_answer(
            channel, topic, body.question_id, body.answers
        )
    except TopicBusyError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    return {"status": "answered"}


@router.post("/channels/{channel_id}/topics/{topic_id}/to-card", status_code=201)
async def topic_to_card(
    channel_id: str,
    topic_id: str,
    request: Request,
    user: CurrentUser = Depends(get_current_user),
) -> dict:
    """One-click topic-to-board-card: the title is prefilled from the root
    message and a topic backlink is attached automatically. Idempotency is
    left to the user (clicking again creates a duplicate card, deletable by
    hand)."""
    channel = _channel_or_404(request, channel_id, user)
    topic = _channels(request).get_topic(topic_id)
    if topic is None or topic["channel_id"] != channel_id:
        raise HTTPException(status_code=404, detail="topic not found")
    root = _channels(request).get_message(topic["root_message_seq"])
    title = (root or {}).get("text", "").strip() or f"#{channel['name']} topic task"
    card = request.app.state.board_store.create_card(
        channel["space_id"],
        title[:100],
        created_by=user.username,
        links=[
            {
                "type": "topic",
                "id": topic_id,
                "channel_id": channel_id,
                "label": title[:40],
            }
        ],
    )
    return {"card": card}


@router.put("/channels/{channel_id}/read")
async def mark_read(
    channel_id: str,
    body: ReadBody,
    request: Request,
    user: CurrentUser = Depends(get_current_user),
) -> dict:
    _channel_or_404(request, channel_id, user)
    _channels(request).set_read(channel_id, user.username, body.seq)
    return {"ok": True}


# ------------------------------------------------------------------- SSE
def _frame(event_type: str, data: dict, seq: Optional[int] = None) -> str:
    lines = []
    if seq is not None:
        lines.append(f"id: {seq}")
    lines.append(f"event: {event_type}")
    lines.append(f"data: {json.dumps(data, ensure_ascii=False)}")
    return "\n".join(lines) + "\n\n"


@router.get("/channels/{channel_id}/stream")
async def channel_stream(
    channel_id: str,
    request: Request,
    since_seq: Optional[int] = Query(default=None),
    user: CurrentUser = Depends(get_current_user),
) -> StreamingResponse:
    """Channel SSE: ``message`` frames carry ``id:`` (the since_seq replay
    axis); ``topic_update`` frames carry no id (snapshot semantics — the
    frontend overwrites by topic.id). Subscribe first, then replay; the
    overlap is deduped by seq."""
    channel = _channel_or_404(request, channel_id, user)
    service = _channel_service(request)
    store = _channels(request)

    async def stream() -> AsyncIterator[str]:
        q = service.subscribe(channel_id)
        last_seq = since_seq if since_seq is not None else -1
        try:
            yield ": connected\n\n"
            # Replay: fetch everything after since_seq in batches.
            cursor = last_seq if last_seq >= 0 else 0
            while True:
                batch = store.list_messages(
                    channel_id, after_seq=cursor, limit=_REPLAY_BATCH
                )
                for m in batch:
                    last_seq = max(last_seq, m["seq"])
                    yield _frame("message", m, seq=m["seq"])
                if len(batch) < _REPLAY_BATCH:
                    break
                cursor = batch[-1]["seq"]
            # Full topic snapshot (one frame; the frontend builds a topic map).
            yield _frame("topics_snapshot", {"topics": service.topic_views(channel)})
            yield _frame("replay_done", {})
            while True:
                if await request.is_disconnected():
                    return
                try:
                    ev = await asyncio.wait_for(q.get(), timeout=_HEARTBEAT_SECONDS)
                except asyncio.TimeoutError:
                    yield ": ping\n\n"
                    continue
                if ev["type"] == "message":
                    seq = ev["data"]["seq"]
                    if seq <= last_seq:
                        continue
                    last_seq = max(last_seq, seq)
                    yield _frame("message", ev["data"], seq=seq)
                else:
                    yield _frame(ev["type"], ev["data"])
        finally:
            service.unsubscribe(channel_id, q)

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )
