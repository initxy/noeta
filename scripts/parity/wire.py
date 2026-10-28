"""Messages API wire helpers shared by distill and replay: reading a recorded
response back into content blocks, writing content blocks out as SSE, and
naming what a request is for."""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from typing import Any

from scripts.parity.facts import Kinds, split_parts

#: Requests named by their system prompt.
SIDE_BY_SYSTEM = {
    "title-prompt": "session-title",
    "plan-name-prompt": "plan-name",
    "classifier-prompt": "auto-mode-classifier",
}
#: Requests that fork a conversation, named by their closing instruction.
FORK_BY_INSTRUCTION = {
    "compact-instruction": "compaction",
    "activity-summary-instruction": "activity-summary",
}
#: The system prompt that makes a request part of the main loop.
MAIN_PROMPT = "main-prompt"
#: Claude Code tool names that noeta offers under another name. Replay calls
#: noeta's tool by it; the comparator reports each rename as a mismatch.
TOOL_NAMES = {"Agent": "Task"}


def parse_response(raw: str) -> dict[str, Any] | None:
    """The assistant message of a recorded response (SSE or JSON):
    ``{"content": [...], "stop_reason": ...}``; ``None`` when it holds none."""
    raw = raw.strip()
    if not raw:
        return None
    if raw.startswith("{"):
        try:
            msg = json.loads(raw)
        except ValueError:
            return None
        if msg.get("type") == "error" or "content" not in msg:
            return None
        return {"content": msg["content"], "stop_reason": msg.get("stop_reason")}
    blocks: dict[int, dict[str, Any]] = {}
    partial: dict[int, str] = {}
    stop = None
    seen = False
    for line in raw.splitlines():
        if not line.startswith("data:"):
            continue
        try:
            ev = json.loads(line[5:].strip())
        except ValueError:
            continue
        t = ev.get("type")
        if t == "message_start":
            seen = True
        elif t == "content_block_start":
            blocks[ev["index"]] = dict(ev["content_block"])
        elif t == "content_block_delta":
            d = ev["delta"]
            b = blocks.setdefault(ev["index"], {"type": "text", "text": ""})
            if d.get("type") == "text_delta":
                b["text"] = b.get("text", "") + d.get("text", "")
            elif d.get("type") == "input_json_delta":
                partial[ev["index"]] = partial.get(ev["index"], "") + d.get("partial_json", "")
            elif d.get("type") == "thinking_delta":
                b["thinking"] = b.get("thinking", "") + d.get("thinking", "")
        elif t == "message_delta":
            stop = (ev.get("delta") or {}).get("stop_reason") or stop
    if not seen:
        return None
    for i, js in partial.items():
        try:
            blocks[i]["input"] = json.loads(js) if js.strip() else {}
        except ValueError:
            blocks[i]["input"] = {"_unparsed": js}
    return {"content": [blocks[i] for i in sorted(blocks)], "stop_reason": stop}


def script_turn(message: Mapping[str, Any]) -> dict[str, Any]:
    """What the model said, as a replay script turn: text and tool calls
    (thinking dropped — it is never resent to the model as-is)."""
    content = []
    for b in message.get("content") or []:
        if b.get("type") == "text" and b.get("text"):
            content.append({"type": "text", "text": b["text"]})
        elif b.get("type") == "tool_use":
            content.append({"type": "tool_use", "name": b.get("name"),
                            "input": b.get("input") or {}})
    return {"content": content, "stop_reason": message.get("stop_reason")}


def sse(content: list[dict[str, Any]], stop_reason: str | None, *, model: str,
        message_id: str = "msg_replay") -> Iterable[bytes]:
    """``content`` streamed as a Messages API SSE response."""
    def ev(name: str, data: dict[str, Any]) -> bytes:
        return f"event: {name}\ndata: {json.dumps(data)}\n\n".encode()

    usage = {"input_tokens": 0, "output_tokens": 0, "cache_read_input_tokens": 0,
             "cache_creation_input_tokens": 0}
    yield ev("message_start", {"type": "message_start", "message": {
        "id": message_id, "type": "message", "role": "assistant", "model": model,
        "content": [], "stop_reason": None, "stop_sequence": None, "usage": usage}})
    for i, b in enumerate(content):
        if b["type"] == "text":
            yield ev("content_block_start", {"type": "content_block_start", "index": i,
                                             "content_block": {"type": "text", "text": ""}})
            yield ev("content_block_delta", {"type": "content_block_delta", "index": i,
                                             "delta": {"type": "text_delta", "text": b["text"]}})
        else:
            yield ev("content_block_start", {"type": "content_block_start", "index": i,
                                             "content_block": {"type": "tool_use", "id": b["id"],
                                                               "name": b["name"], "input": {}}})
            yield ev("content_block_delta", {
                "type": "content_block_delta", "index": i,
                "delta": {"type": "input_json_delta", "partial_json": json.dumps(b["input"])}})
        yield ev("content_block_stop", {"type": "content_block_stop", "index": i})
    yield ev("message_delta", {"type": "message_delta",
                               "delta": {"stop_reason": stop_reason, "stop_sequence": None},
                               "usage": {"output_tokens": 1}})
    yield ev("message_stop", {"type": "message_stop"})


def _texts(message: Mapping[str, Any]) -> list[str]:
    c = message.get("content")
    if isinstance(c, str):
        return [c]
    return [b.get("text") or "" for b in c or [] if b.get("type") == "text"]


def system_kinds(body: Mapping[str, Any], kinds: Kinds) -> list[str]:
    sysb = body.get("system")
    blocks = [{"text": sysb}] if isinstance(sysb, str) else (sysb or [])
    return [kinds.name(b.get("text") or "") for b in blocks]


def request_kind(path: str, body: Mapping[str, Any], kinds: Kinds) -> str:
    """What a request is for: ``main``, ``subagent:<prompt kind>``, a fork of
    either (``compaction``, ``activity-summary``) or a side request."""
    if "count_tokens" in path:
        return "count-tokens"
    if body.get("max_tokens") == 1:
        return "quota-probe"
    sysk = system_kinds(body, kinds)
    for k in sysk:
        if k in SIDE_BY_SYSTEM:
            return SIDE_BY_SYSTEM[k]
    messages = list(body.get("messages") or [])
    last_user = next((m for m in reversed(messages) if m.get("role") == "user"), None)
    if last_user is not None:
        for t in _texts(last_user):
            for part in split_parts(t, kinds.starts):
                fork = FORK_BY_INSTRUCTION.get(kinds.name(part))
                if fork:
                    return fork if MAIN_PROMPT in sysk else f"{fork}:subagent"
    if messages and any(kinds.name(p) == "webfetch-page"
                        for t in _texts(messages[0]) for p in split_parts(t, kinds.starts)):
        return "webfetch-digest"
    if MAIN_PROMPT in sysk:
        return "main"
    prompts = [k for k in sysk if k.endswith("-prompt")]
    if prompts and body.get("tools"):
        return f"subagent:{prompts[-1]}"
    return "other:" + (sysk[-1] if sysk else "no-system")


def user_texts(body: Mapping[str, Any]) -> list[str]:
    """Every user/system-role text block of a request's messages."""
    out: list[str] = []
    for m in body.get("messages") or []:
        if m.get("role") != "assistant":
            out += _texts(m)
    return out
