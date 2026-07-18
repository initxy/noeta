"""Channel service: message stream + @Agent topics + status projection.

Data flow (the same routing shape as an external group-chat integration, with
the entry point swapped to the in-platform API):

- Post a message (API thread, on the event loop) → persist to
  `channel_messages` → hub broadcasts a message frame; on an @Agent hit →
  lazily create the channel session (**one channel = one persistent
  session**, sentinel user `__channel__`, long-lived container) → topic = a
  new root task (a `session_tasks` row, reusing the multi-task
  shared-container mechanism) → `channel_topics` records the mapping.
- Follow-ups inside a topic are routed to that task via the mapping (append
  a message / answer a follow-up question), no @ required.
- Status projection: a per-session watcher subscribes to AgentService's
  UIEvent queue (same source as the frontend SSE) and demultiplexes by the
  `_task` tag; at turn boundaries it writes the task status + a clipped
  last-reply preview back to `channel_topics` and pushes a topic_update frame
  to the channel hub (the topic card's data source). Agent replies are **not
  persisted to channel_messages** — the topic view replays per-task from the
  EventLog.

Channel context (the core of what distinguishes a channel from "several
independent sessions"): a new topic's goal is prefixed with recent
main-stream messages + an index of past topics (root message + clipped last
reply, zero LLM); older content is paged in on demand through the
`channel_read_history` / `channel_read_topic` tools.

The hub is an in-process broadcast (single-process deployment assumption):
every publish happens on the event-loop thread (API handler / watcher task),
so there are no cross-thread writes.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Optional

from noeta.agent.config import Settings
from noeta.agent.store.channels import ChannelStore

logger = logging.getLogger(__name__)

#: Sentinel user for channel sessions: belongs to no real user; the "my
#: sessions" list excludes it by prefix (store.sessions.list_for_space), and
#: visibility is decided by space membership.
CHANNEL_SESSION_USER = "__channel__"

#: Context injection budgets: recent main-stream message count / character
#: budget, topic index count, preview clip length
_CTX_MESSAGE_LIMIT = 50
_CTX_MESSAGE_CHARS = 4000
_CTX_TOPIC_LIMIT = 20
_PREVIEW_CHARS = 200


class TopicBusyError(Exception):
    """The topic cannot accept a delivery right now (running / startup in
    flight / follow-up context lost)."""


class ChannelService:
    """Channel messages / topic driving / status projection + in-process SSE hub."""

    def __init__(
        self,
        *,
        settings: Settings,
        service: Any,
        session_store: Any,
        space_store: Any,
        channel_store: ChannelStore,
        agent_config_store: Any,
    ) -> None:
        self._settings = settings
        self._service = service
        self._sessions = session_store
        self._spaces = space_store
        self._channels = channel_store
        self._agent_config = agent_config_store
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._watchers: dict[str, asyncio.Task] = {}
        #: task_id → payload of the most recent question event (used to
        #: answer follow-ups with plain text inside a topic)
        self._pending_questions: dict[str, dict] = {}
        #: channel_id → subscriber queues (channel SSE); read/written only on
        #: the event-loop thread
        self._subs: dict[str, set[asyncio.Queue]] = {}

    # ---------------------------------------------------------------- lifecycle
    def start(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop
        # Restart recovery: re-attach watchers for channel sessions that have
        # topics (in-flight tasks are re-driven automatically by the
        # WorkerLoop; without the watcher the status projection is lost)
        try:
            for sid in self._channels.channels_with_topics():
                self._ensure_watcher(sid)
        except Exception:  # noqa: BLE001 - recovery failure must not block startup
            logger.exception("channel watcher recovery failed")

    async def stop(self) -> None:
        for task in self._watchers.values():
            task.cancel()
        for task in list(self._watchers.values()):
            try:
                await task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        self._watchers.clear()

    # ---------------------------------------------------------------- hub
    def subscribe(self, channel_id: str) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue()
        self._subs.setdefault(channel_id, set()).add(q)
        return q

    def unsubscribe(self, channel_id: str, q: asyncio.Queue) -> None:
        queues = self._subs.get(channel_id)
        if queues is not None:
            queues.discard(q)
            if not queues:
                self._subs.pop(channel_id, None)

    def _publish(self, channel_id: str, event: dict) -> None:
        for q in list(self._subs.get(channel_id, ())):
            q.put_nowait(event)

    def _publish_threadsafe(self, channel_id: str, event: dict) -> None:
        if self._loop is None:
            return
        self._loop.call_soon_threadsafe(self._publish, channel_id, event)

    # ------------------------------------------------------------ topic views
    def topic_views(self, channel: dict) -> list[dict]:
        """Channel topic snapshot (task_id / status read live from
        session_tasks, the single source of truth)."""
        topics = self._channels.list_topics(channel["id"])
        if not topics:
            return []
        tasks_by_node: dict[int, dict] = {}
        if channel.get("session_id"):
            for t in self._sessions.list_session_tasks(channel["session_id"]):
                tasks_by_node[t["node_index"]] = t
        return [self._topic_view(t, tasks_by_node.get(t["node_index"])) for t in topics]

    def _topic_view(self, topic: dict, task_row: Optional[dict]) -> dict:
        return {
            **topic,
            # A seed in flight (no task row yet) also counts as running: the
            # topic was just initiated
            "task_id": (task_row or {}).get("task_id"),
            "status": (task_row or {}).get("status") or "running",
        }

    def _topic_view_by_node(self, channel: dict, node_index: int) -> Optional[dict]:
        topic = self._channels.get_topic_by_node(channel["id"], node_index)
        if topic is None:
            return None
        task_row = self._task_row_by_node(channel, node_index)
        return self._topic_view(topic, task_row)

    def _task_row_by_node(self, channel: dict, node_index: int) -> Optional[dict]:
        if not channel.get("session_id"):
            return None
        return next(
            (
                t
                for t in self._sessions.list_session_tasks(channel["session_id"])
                if t["node_index"] == node_index
            ),
            None,
        )

    # ------------------------------------------------------------ message entry
    def post_message(
        self, channel: dict, username: str, text: str, mention_agent: bool
    ) -> tuple[dict, Optional[dict]]:
        """Post to the main stream; with mention_agent, start a topic
        synchronously. Returns (message, topic_view|None).

        Called on the event-loop thread (API handler). When starting the
        topic fails, the message is kept, the topic is not created, and the
        exception propagates up to the API layer.
        """
        message = self._channels.add_message(channel["id"], username, text)
        topic_view: Optional[dict] = None
        if mention_agent:
            try:
                topic_view = self._start_topic(channel, message, username)
            except Exception:
                # Topic start failed: drop the root marker, keep the message
                # as human chat, and re-raise (the API returns 5xx/409)
                self._publish(channel["id"], {"type": "message", "data": message})
                raise
            message = {**message, "topic_id": topic_view["id"]}
        self._publish(channel["id"], {"type": "message", "data": message})
        if topic_view is not None:
            self._publish(channel["id"], {"type": "topic_update", "data": topic_view})
        return message, topic_view

    # ------------------------------------------------------------ topic driving
    def _start_topic(self, channel: dict, message: dict, username: str) -> dict:
        session = self._ensure_session(channel)
        # The channel may have just had session_id backfilled: subsequent
        # paths read the latest row
        channel = self._channels.get_channel(channel["id"]) or channel
        rows = self._sessions.list_session_tasks(session.id)
        node_index = max((r["node_index"] for r in rows), default=-1) + 1
        goal = self._build_topic_goal(channel, message, username)
        config = self._agent_config.get(channel["space_id"])
        self._service.start_workflow_node(
            session, node_index, goal, effort=config["default_effort"] or None
        )
        topic = self._channels.add_topic(
            channel["id"], message["seq"], node_index, username
        )
        self._channels.set_message_topic(message["seq"], topic["id"])
        self._ensure_watcher(session.id)
        return self._topic_view(topic, None)

    def topic_message(
        self, channel: dict, topic: dict, username: str, text: str
    ) -> None:
        """Follow-up inside a topic (no @ needed): while waiting, answer the
        pending question; otherwise append a message to the task. Busy /
        undeliverable raises TopicBusyError."""
        from noeta.agent.host.service import SessionBusyError

        session = (
            self._sessions.get(channel["session_id"])
            if channel.get("session_id")
            else None
        )
        row = self._task_row_by_node(channel, topic["node_index"])
        if session is None or row is None or not row["task_id"]:
            raise TopicBusyError("The topic is still starting; try again shortly")
        task_id = row["task_id"]
        content = f"[{username}] {text}"
        try:
            if row["status"] == "waiting":
                pending = self._pending_questions.pop(task_id, None)
                if pending is None:
                    raise TopicBusyError(
                        "This topic has a follow-up question that cannot be"
                        " recovered; @Agent in the main stream to start a new topic"
                    )
                answers = {
                    q["id"]: {"text": text}
                    for q in pending.get("questions", [])
                    if q.get("id")
                }
                self._service.answer_task(
                    session, task_id, pending.get("question_id", ""), answers
                )
            else:
                self._service.send_task_message(session, task_id, content)
        except SessionBusyError as exc:
            raise TopicBusyError(
                "The agent is still handling the previous message; try again shortly"
            ) from exc

    def topic_answer(
        self, channel: dict, topic: dict, question_id: str, answers: dict
    ) -> None:
        """Structured follow-up answer inside a topic (frontend QuestionCard
        submit)."""
        from noeta.agent.host.service import SessionBusyError

        session = (
            self._sessions.get(channel["session_id"])
            if channel.get("session_id")
            else None
        )
        row = self._task_row_by_node(channel, topic["node_index"])
        if session is None or row is None or not row["task_id"]:
            raise TopicBusyError("The topic is still starting; try again shortly")
        self._pending_questions.pop(row["task_id"], None)
        try:
            self._service.answer_task(session, row["task_id"], question_id, answers)
        except SessionBusyError as exc:
            raise TopicBusyError(
                "There is no pending follow-up question to answer"
            ) from exc

    # ------------------------------------------------------------ session & goal
    def _ensure_session(self, channel: dict) -> Any:
        """The channel's persistent session: reuse when it exists, otherwise
        create it under the sentinel user (LLM title generation skipped)."""
        session = None
        if channel.get("session_id"):
            session = self._sessions.get(channel["session_id"])
        if session is not None:
            return session
        from noeta.agent.models_config import get_default_model, get_models

        config = self._agent_config.get(channel["space_id"])
        model = config["default_model"] or ""
        known = {m.id for m in get_models(self._settings)}
        if model not in known:
            model = get_default_model(self._settings).id
        session = self._sessions.create(
            CHANNEL_SESSION_USER, model, channel["space_id"]
        )
        title = f"Channel: {channel['name']}"
        self._sessions.update(session.id, title=title[:40], title_generated=1)
        session = self._sessions.get(session.id) or session
        self._channels.update_channel(channel["id"], session_id=session.id)
        return session

    def _build_topic_goal(self, channel: dict, message: dict, username: str) -> str:
        """Topic goal = channel identity section + channel context (recent
        human chat + topic index) + the current request."""
        space = self._spaces.get_space(channel["space_id"]) or {}
        lines = [
            "[Channel topic]",
            f"- Channel: {space.get('name', '')} / #{channel['name']}",
            f"- Initiator: {username} (space member)",
            "- You are replying inside a channel topic: keep replies concise,"
            " lead with the conclusion, search only when needed, and avoid"
            " long process narration.",
            '- The "Recent channel messages" and "Past channel topics" below'
            " are background context; page in older content with the"
            " channel_read_history / channel_read_topic tools.",
        ]
        recent = self._recent_context(channel, exclude_seq=message["seq"])
        if recent:
            lines += ["", "## Recent channel messages", recent]
        index = self._topic_index(channel)
        if index:
            lines += ["", "## Past channel topics", index]
        lines += ["", "## Current request", f"[{username}] {message['text']}"]
        return "\n".join(lines)

    def _recent_context(self, channel: dict, exclude_seq: int) -> str:
        msgs = self._channels.list_messages(channel["id"], limit=_CTX_MESSAGE_LIMIT)
        blocks = [
            f"[{m['author']}] {m['text']}" for m in msgs if m["seq"] != exclude_seq
        ]
        if not blocks:
            return ""
        # Over budget, keep the tail (the most recent messages are the most
        # valuable)
        out: list[str] = []
        budget = _CTX_MESSAGE_CHARS
        for b in reversed(blocks):
            if budget - len(b) < 0:
                break
            out.append(b)
            budget -= len(b)
        out.reverse()
        return "\n".join(out)

    def _topic_index(self, channel: dict) -> str:
        views = self.topic_views(channel)[-_CTX_TOPIC_LIMIT:]
        lines = []
        for v in views:
            root = self._root_text(channel["id"], v["root_message_seq"])
            entry = f"- (topic_id={v['id']}, status={v['status']}) {root}"
            if v["last_reply_preview"]:
                entry += f" → last reply: {v['last_reply_preview']}"
            lines.append(entry)
        return "\n".join(lines)

    def _root_text(self, channel_id: str, seq: int) -> str:
        msg = self._channels.get_message(seq)
        if msg is None or msg["channel_id"] != channel_id:
            return "(message no longer exists)"
        return f"[{msg['author']}] \"{msg['text'][:_PREVIEW_CHARS]}\""

    # ------------------------------------------------------- tool read surface
    def resolve_channel_for_task(self, task_id: str) -> Optional[dict]:
        """task → channel (ownership resolution for agent tools; returns None
        for non-channel-topic tasks)."""
        row = self._sessions.get_session_task_by_task_id(task_id)
        if row is None:
            return None
        return self._channels.get_channel_by_session(row["session_id"])

    def topic_link_for_task(self, task_id: str) -> Optional[dict]:
        """task → channel-topic backlink (the links element on board cards);
        returns None for non-channel topics."""
        row = self._sessions.get_session_task_by_task_id(task_id)
        if row is None:
            return None
        channel = self._channels.get_channel_by_session(row["session_id"])
        if channel is None:
            return None
        topic = self._channels.get_topic_by_node(channel["id"], row["node_index"])
        if topic is None:
            return None
        root = self._channels.get_message(topic["root_message_seq"])
        label = (root or {}).get("text", "")[:40] or f"#{channel['name']} topic"
        return {
            "type": "topic",
            "id": topic["id"],
            "channel_id": channel["id"],
            "label": label,
        }

    def read_history(
        self, channel: dict, before_seq: Optional[int], limit: int
    ) -> list[dict]:
        return self._channels.list_messages(
            channel["id"], before_seq=before_seq, limit=min(max(limit, 1), 200)
        )

    def read_topic(self, channel: dict, topic_id: str) -> Optional[str]:
        """Full conversation transcript of a topic (channel_read_topic tool);
        None for an invalid topic."""
        topic = self._channels.get_topic(topic_id)
        if topic is None or topic["channel_id"] != channel["id"]:
            return None
        row = self._task_row_by_node(channel, topic["node_index"])
        if row is None or not row["task_id"]:
            return ""
        return self._service.task_transcript_sync(row["task_id"])

    # ---------------------------------------------------------------- watcher
    def _ensure_watcher(self, session_id: str) -> None:
        if self._loop is None:
            return
        existing = self._watchers.get(session_id)
        if existing is not None and not existing.done():
            return
        self._watchers[session_id] = self._loop.create_task(
            self._watch(session_id), name=f"channel-watch-{session_id}"
        )

    async def _watch(self, session_id: str) -> None:
        """Subscribe to the channel session's UIEvent stream and project each
        topic task's status and reply preview into channel_topics + the
        channel hub (the topic card's data source)."""
        q = self._service.subscribe(session_id)
        buf: dict[str, list[str]] = {}
        loop = asyncio.get_running_loop()
        try:
            while True:
                ev = await q.get()
                etype = getattr(ev, "type", "")
                data = getattr(ev, "data", None) or {}
                task_id = str(data.get("_task") or "")
                if not task_id:
                    continue
                if etype == "assistant_text":
                    text = str(data.get("text") or "")
                    if text:
                        buf.setdefault(task_id, []).append(text)
                elif etype == "question":
                    self._pending_questions[task_id] = data
                    self._project_topic(session_id, task_id)
                elif etype == "turn_started":
                    self._project_topic(session_id, task_id)
                elif etype == "turn_finished":
                    text = "\n\n".join(buf.pop(task_id, ())).strip()
                    if not text:
                        text = await loop.run_in_executor(
                            None, self._service.latest_assistant_reply, task_id
                        ) or ""
                    self._project_topic(session_id, task_id, preview=text)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            logger.exception("channel watcher exited abnormally: session=%s", session_id)
        finally:
            self._service.unsubscribe(session_id, q)
            self._watchers.pop(session_id, None)

    def _project_topic(
        self, session_id: str, task_id: str, preview: Optional[str] = None
    ) -> None:
        """task → topic-row projection + hub broadcast (called on the event
        loop; status reads the session_tasks source of truth —
        _update_status persists before the event push)."""
        channel = self._channels.get_channel_by_session(session_id)
        if channel is None:
            return
        row = self._sessions.get_session_task_by_task_id(task_id)
        if row is None:
            return
        topic = self._channels.get_topic_by_node(channel["id"], row["node_index"])
        if topic is None:
            return
        if preview:
            clipped = preview.strip()[:_PREVIEW_CHARS]
            self._channels.update_topic_preview(topic["id"], clipped)
            topic = {**topic, "last_reply_preview": clipped}
        self._publish(
            channel["id"],
            {"type": "topic_update", "data": self._topic_view(topic, row)},
        )


__all__ = ["ChannelService", "TopicBusyError", "CHANNEL_SESSION_USER"]
