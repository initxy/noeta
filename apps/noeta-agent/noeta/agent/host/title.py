"""Async LLM generation of session titles.

A small home-grown feature (upstream noeta only truncates the goal's first
line): after the first turn of a conversation ends, a background thread asks
the LLM for a concise title of at most 16 characters. The truncated title
remains the instant fallback (visible before generation / on failure).

Deep module: the only public surface is the pure function `generate_title`;
provider detection + a direct httpx call to the gateway's Responses endpoint +
result cleanup all hide inside. The mock provider returns None directly
(tests do not depend on a real LLM; they monkeypatch this function to inject a
fixed title).
"""
from __future__ import annotations

import logging
from typing import Optional

import httpx

from noeta.agent.config import Settings

logger = logging.getLogger(__name__)

_TITLE_MAXLEN = 16
_TITLE_TIMEOUT = 30.0  # titles are tiny; no need for llm_request_timeout (300s)

_INSTRUCTIONS = (
    "Generate one concise title for this conversation in the user's language, "
    "no more than 16 characters, summarizing the user's intent. "
    "Output the title itself only - no quotes, no trailing punctuation, no "
    "book-title marks, and no prefix, suffix, or explanation."
)


def _clean_title(raw: str) -> str:
    """Clean the model output: strip quotes / trailing punctuation / newlines /
    extra whitespace, truncate over-long titles, return "" when empty."""
    t = raw.strip()
    # Strip paired / stray quotes and book-title marks, then edge punctuation
    for ch in ('"', "'", "“", "”", "‘", "’", "《", "》", "「", "」", "『", "』", "`"):
        t = t.replace(ch, "")
    t = t.replace("\r", " ").replace("\n", " ").strip()
    # Collapse internal whitespace
    t = " ".join(t.split())
    # Strip fullwidth / ASCII sentence-ending punctuation at both edges
    t = t.strip(" 。.、,，;；:：!！?？")
    if len(t) > _TITLE_MAXLEN:
        t = t[:_TITLE_MAXLEN]
    return t.strip()


def _build_prompt(
    first_message: str,
    assistant_reply: Optional[str],
) -> str:
    parts = [f"User's first message:\n{first_message.strip()[:600]}"]
    if assistant_reply:
        parts.append(f"\nAssistant reply (excerpt):\n{assistant_reply.strip()[:300]}")
    return "\n".join(parts)


def _extract_output_text(payload: dict) -> str:
    """Extract output_text from an OpenAI Responses payload (same shape as the
    provider's _parse_response)."""
    output = payload.get("output")
    if not isinstance(output, list):
        return ""
    texts: list[str] = []
    for item in output:
        if not isinstance(item, dict) or item.get("type") != "message":
            continue
        for seg in item.get("content") or []:
            if isinstance(seg, dict) and seg.get("type") == "output_text":
                text = seg.get("text")
                if isinstance(text, str):
                    texts.append(text)
    return "".join(texts)


def generate_title(
    settings: Settings,
    first_message: str,
    assistant_reply: Optional[str],
    task_id: str,
) -> Optional[str]:
    """Generate a session title; return None on failure / when not applicable
    (the caller decides whether to persist or record the failure).

    - mock provider (no gateway credentials): return None directly, no request.
    - openai: direct httpx call to the settings' Responses endpoint (occupies
      neither the jobs-worker nor the noeta WorkerLoop); carries the
      ``x-session-id: <task_id>`` header so gateways that key prompt-cache
      affinity on a stable per-task session id hit the same backend account
      (same scheme as HostConfig.provider_headers).
    """
    if settings.effective_provider != "openai":
        return None
    if not first_message.strip():
        return None

    from noeta.agent.models_config import get_default_model

    # title_model defaults to gpt-5.4 (config.py); only an explicitly empty
    # setting falls back to the conversation default model.
    model = settings.title_model or get_default_model(settings).id
    endpoint = settings.llm_base_url.rstrip("/") + "/responses"
    body = {
        "model": model,
        "input": [
            {
                "role": "user",
                "content": [
                    {
                        "type": "input_text",
                        "text": _build_prompt(first_message, assistant_reply),
                    }
                ],
            }
        ],
        "instructions": _INSTRUCTIONS,
        "store": False,
        # Explicitly disable reasoning: a title is a tiny non-interactive task
        # that needs no thinking. Reasoning models (gpt-5.5) default to medium
        # and burn the whole max_output_tokens budget on reasoning_tokens,
        # squeezing out the actual output (status=incomplete, no message in
        # output) - the title would always come back empty. gpt-5.4's effort
        # value set is none/low/medium/high/xhigh (no minimal).
        "reasoning": {"effort": "none"},
        # Titles are short; cap output tokens to save money and time
        # (Responses uses max_output_tokens)
        "max_output_tokens": 64,
    }
    headers = {
        "api-key": settings.llm_api_key,
        "content-type": "application/json",
        # Matches provider_headers: a stable per-task session id pins the
        # request to the same backend account for prompt-cache affinity
        "x-session-id": task_id,
    }
    try:
        resp = httpx.post(
            endpoint, json=body, headers=headers, timeout=_TITLE_TIMEOUT
        )
        resp.raise_for_status()
        payload = resp.json()
    except Exception:  # noqa: BLE001 - network / gateway / parse failures all count as generation failure
        logger.exception("title generation request failed")
        return None

    title = _clean_title(_extract_output_text(payload))
    return title or None
