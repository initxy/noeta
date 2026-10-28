"""A server's ``elicitation/create`` as the kernel's question form, and the
person's answer back as the elicitation result.

* **Form mode**: each field of ``requestedSchema`` is one question, its
  title (else name) as the header and its description as the text; the
  server's message opens the first. A boolean field offers ``Yes`` / ``No``,
  an enum (``enum``, ``oneOf`` with ``const``/``title``, or an array of
  them, which allows several) offers its values, anything else takes typed
  text. An optional field also offers ``Skip``. A schema with no field asks
  to accept or decline.
* **URL mode**: one question shows the message and the URL to open, with
  ``Accept`` / ``Decline``.

The answer is ``accept`` with the typed ``content``; a dismissed question
is ``decline``, a withdrawn one (the turn stopped) ``cancel``. A value that
does not fit its field (a number that is not one) declines.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from noeta.core.events import Answer
from noeta.core.questions import AnswerValidationError, read_answer
from noeta.core.tool import Park

__all__ = ["SKIP", "elicitation_questions", "elicitation_result"]

SKIP = "Skip"
_YES, _NO = "Yes", "No"
_ACCEPT, _DECLINE = "Accept", "Decline"
_DESCRIPTION_CAP = 400


class _Unfit(ValueError):
    """An answer that does not fit its field."""


def _options(field: Mapping[str, Any]) -> list[tuple[str, Any]]:
    """``(label, value)`` of an enum-like field (or its items)."""
    if field.get("type") == "array" and isinstance(field.get("items"), Mapping):
        field = field["items"]
    if field.get("type") == "boolean":
        return [(_YES, True), (_NO, False)]
    for key in ("oneOf", "anyOf"):
        choices = field.get(key)
        if isinstance(choices, list) and choices:
            return [(str(c.get("title") or c.get("const")), c.get("const"))
                    for c in choices if isinstance(c, Mapping) and "const" in c]
    values = field.get("enum")
    if isinstance(values, list) and values:
        names = field.get("enumNames")
        labels = names if isinstance(names, list) and len(names) == len(values) else values
        return [(str(label), value) for label, value in zip(labels, values, strict=True)]
    return []


def _fields(params: Mapping[str, Any]) -> list[tuple[str, Mapping[str, Any], bool]]:
    schema = params.get("requestedSchema")
    props = schema.get("properties") if isinstance(schema, Mapping) else None
    if not isinstance(props, Mapping):
        return []
    required = set(schema.get("required") or []) if isinstance(schema, Mapping) else set()
    return [(str(name), field if isinstance(field, Mapping) else {}, name in required)
            for name, field in props.items()]


def elicitation_questions(server: str, params: Mapping[str, Any]) -> list[dict[str, Any]]:
    """The questions that put ``params`` to the person (see the module
    docstring); ``server`` names who asks."""
    message = str(params.get("message") or "").strip()
    opening = f"The MCP server {server!r} asks: {message}" if message \
        else f"The MCP server {server!r} asks for input."
    if params.get("mode") == "url":
        return [{"question": f"{opening}\n\nOpen {params.get('url')}", "header": server,
                 "options": [{"label": _ACCEPT}, {"label": _DECLINE}]}]
    fields = _fields(params)
    if not fields:
        return [{"question": opening, "header": server,
                 "options": [{"label": _ACCEPT}, {"label": _DECLINE}]}]
    out: list[dict[str, Any]] = []
    for i, (name, field, required) in enumerate(fields):
        title = str(field.get("title") or name)
        about = str(field.get("description") or "")[:_DESCRIPTION_CAP]
        text = f"{title}: {about}" if about else title
        kind = field.get("type")
        if kind in ("number", "integer"):
            text += f" ({kind})"
        options = [{"label": label} for label, _ in _options(field)]
        if not required:
            options.append({"label": SKIP})
        labels = [o["label"] for o in options]
        if len(set(labels)) != len(labels):
            options = []  # labels that cannot be told apart: typed text instead
        out.append({"question": f"{opening}\n\n{text}" if i == 0 else text,
                    "header": title[:40], "options": options,
                    "multiSelect": field.get("type") == "array"})
    return out


def _value(field: Mapping[str, Any], answer: Mapping[str, Any]) -> Any:
    selected = [s for s in answer.get("selected") or [] if s != SKIP]
    other = answer.get("other")
    options = dict(_options(field))
    if field.get("type") == "array":
        values = [options[s] for s in selected if s in options]
        if isinstance(other, str) and other.strip():
            values += [v.strip() for v in other.split(",") if v.strip()]
        return values
    if selected:
        return options.get(selected[0], selected[0])
    text = (other or "").strip()
    kind = field.get("type")
    try:
        if kind == "integer":
            return int(text)
        if kind == "number":
            return float(text) if any(c in text for c in ".eE") else int(text)
    except ValueError:
        raise _Unfit(text) from None
    if kind == "boolean":
        if text.lower() in ("yes", "y", "true", "1"):
            return True
        if text.lower() in ("no", "n", "false", "0"):
            return False
        raise _Unfit(text)
    return text


def elicitation_result(params: Mapping[str, Any], outcome: Answer | None,
                       asked: Park) -> dict[str, Any]:
    """The elicitation result for what the person did with ``asked``."""
    if outcome is None:
        return {"action": "cancel"}
    try:
        answers = read_answer(outcome, asked)
    except AnswerValidationError:
        return {"action": "decline"}
    if answers is None:
        return {"action": "decline"}
    fields = _fields(params)
    if params.get("mode") == "url" or not fields:
        return {"action": "accept" if answers["0"]["selected"] != [_DECLINE] else "decline"}
    content: dict[str, Any] = {}
    for i, (name, field, _required) in enumerate(fields):
        answer = answers.get(str(i)) or {}
        if answer.get("selected") == [SKIP]:
            continue
        try:
            content[name] = _value(field, answer)
        except _Unfit:
            return {"action": "decline"}
    return {"action": "accept", "content": content}
