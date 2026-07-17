"""Standalone handoff generation on advance.

A deep module: the outside sees one pure function, `generate_handoff` — it
takes four inputs (the previous task's transcript + the next node's full
template prompt + the param definitions + fixed instructions), makes one
direct httpx call to the OpenAI-compatible Responses API, and produces a
``HandoffResult``:

- ``params``: per-param suggested values (absent from the context → None —
  **fabrication is forbidden**, enforced by a hard prompt constraint + the
  parsing layer normalizing non-strings to None)
- ``summary``: the handoff summary (key conclusions the params cannot hold,
  at most 200 words)
- ``handoff_doc``: the full handoff document (markdown; frontend preview +
  saved into the workspace)
- ``degraded``: True when the LLM is unavailable / output parsing fails
  twice — an all-empty result is returned and the advance flow is not
  blocked (the user fills the form by hand; the agreed degradation)

No noeta task is started: no tool loop, highly deterministic, retries on
failure are simple (the same direct-call style as title.py). The mock
provider (no gateway credentials) degrades immediately — tests inject fixed
results via monkeypatch.
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Optional

import httpx

from noeta.agent.config import Settings

logger = logging.getLogger(__name__)

_HANDOFF_TIMEOUT = 90.0
_MAX_OUTPUT_TOKENS = 8192  # generating the full document needs more tokens

_INSTRUCTIONS = """\
You are the workflow handoff assistant. Below are the complete record of the previous stage (conversation + tool-call summaries) and the task template for the next stage.

Your job:

## 1. Write the handoff document (handoff_doc)

Write a complete handoff document in markdown, covering:

**What the previous stage accomplished**
- Which tasks were completed (key decisions, artifacts, findings)
- Which files/documents were produced (list paths or links)
- What the tool calls revealed (which files were read, which commands were run, what the results were)

**Key decisions and conclusions**
- Technology or approach decisions made explicitly in the conversation
- Problems or risks discovered

**Open items**
- What remains unresolved
- Pitfalls or constraints the next stage must watch for

**Advice for the next stage**
- Where to start
- What to prioritize

## 2. Extract the next stage's params (params)

Extract the value of each param the next stage needs, one by one:
- Use only information that explicitly appears in the context
- A param whose value is absent from the context must be null — **fabricating, guessing, or filling in values is strictly forbidden**

## 3. Write the handoff summary (summary)

One condensed summary of at most 200 words, for the next stage's goal to quote directly.

---

Output exactly one JSON object, with no other text and no code fences:
```json
{
  "handoff_doc": "<full markdown document>",
  "params": {"<param name>": "<string value or null>", ...},
  "summary": "<200-word summary>"
}
```
params must cover every param name listed below."""


@dataclass
class HandoffResult:
    params: dict[str, Optional[str]] = field(default_factory=dict)
    summary: str = ""
    handoff_doc: str = ""  # the full handoff document, markdown
    degraded: bool = False


def _empty_result(param_names: list[str], degraded: bool = True) -> HandoffResult:
    return HandoffResult(
        params={name: None for name in param_names},
        summary="",
        handoff_doc="",
        degraded=degraded,
    )


def build_handoff_prompt(
    transcript: str, next_prompt: str, params: list[dict]
) -> str:
    """Assemble the input: next-node template + param definitions + the full
    transcript of the previous stage (tool calls included)."""
    param_lines = "\n".join(
        f"- {p['name']}: {p.get('description') or '(no description)'}" for p in params
    ) or "(this stage has no params)"
    return (
        f"# Next-stage task template (placeholders verbatim)\n\n{next_prompt}\n\n"
        f"# Params to extract for the next stage\n\n{param_lines}\n\n"
        f"# Complete record of the previous stage (conversation + tool-call summaries)\n\n{transcript}"
    )


def _extract_output_text(payload: dict) -> str:
    """Pull output_text out of an OpenAI Responses payload (same as
    title.py)."""
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


def _parse_result(raw: str, param_names: list[str]) -> Optional[HandoffResult]:
    """Parse the model's JSON output; returns None on structural mismatch
    (the caller retries / degrades)."""
    text = raw.strip()
    # Tolerance: strip a possible ```json fence
    m = re.search(r"\{.*\}", text, re.DOTALL)
    if m is None:
        return None
    try:
        data = json.loads(m.group(0))
    except ValueError:
        return None
    if not isinstance(data, dict):
        return None
    raw_params = data.get("params")
    if not isinstance(raw_params, dict):
        return None
    params: dict[str, Optional[str]] = {}
    for name in param_names:
        val = raw_params.get(name)
        # Non-strings / empty strings all normalize to None (not extracted);
        # no odd-shaped values reach the form
        params[name] = val.strip() if isinstance(val, str) and val.strip() else None
    summary = data.get("summary")
    handoff_doc = data.get("handoff_doc")
    return HandoffResult(
        params=params,
        summary=summary.strip() if isinstance(summary, str) else "",
        handoff_doc=handoff_doc.strip() if isinstance(handoff_doc, str) else "",
        degraded=False,
    )


def generate_handoff(
    settings: Settings,
    transcript: str,
    next_prompt: str,
    params: list[dict],
    model: str,
    session_id: str,
) -> HandoffResult:
    """Generate the handoff: one LLM call + one retry on parse failure +
    all-empty degradation (never raises)."""
    param_names = [p["name"] for p in params]
    if settings.effective_provider != "openai":
        # mock / credentials not configured: degrade to an empty form (the
        # advance is not blocked; tests monkeypatch this function)
        return _empty_result(param_names)

    endpoint = settings.llm_base_url.rstrip("/") + "/responses"
    body = {
        "model": model,
        "input": [
            {
                "role": "user",
                "content": [{
                    "type": "input_text",
                    "text": build_handoff_prompt(transcript, next_prompt, params),
                }],
            }
        ],
        "instructions": _INSTRUCTIONS,
        "store": False,
        "max_output_tokens": _MAX_OUTPUT_TOKENS,
    }
    headers = {
        "api-key": settings.llm_api_key,
        "content-type": "application/json",
        # The same account-routing header the chat path uses (some gateways
        # route on it; the extra value is JSON text)
        "extra": json.dumps({"session_id": f"handoff-{session_id}"}),
    }

    for attempt in (1, 2):
        try:
            resp = httpx.post(
                endpoint, json=body, headers=headers, timeout=_HANDOFF_TIMEOUT
            )
            resp.raise_for_status()
            payload = resp.json()
        except Exception:  # noqa: BLE001 - network/gateway failure: retry once, then degrade
            logger.exception("handoff generation request failed (attempt %s)", attempt)
            continue
        result = _parse_result(_extract_output_text(payload), param_names)
        if result is not None:
            return result
        logger.warning("handoff output parse failed (attempt %s)", attempt)
    return _empty_result(param_names)
