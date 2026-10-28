"""Facts of one Messages API request — what parity compares, never the text.

A request (Claude Code's, from a recording, or noeta's, from a replay) is
reduced to: model-agnostic top-level parameters, the beta header set, system
block lengths/hashes/cache marks, the tool list with each schema's shape
(descriptions replaced by length + hash), and the message layout: roles,
block types, cache marks and the *kind* of each text part.

A text part's kind is a short stable id: a leading tag (``<system-reminder>``,
``<command-name>`` ...) or a hash of its normalized opening words. Readable
names for Claude Code's kinds (our names, see ``names.json``) are applied on
top; a kind with no name shows as ``p:<hash>``. Hashes of whole texts are kept
so a new recording shows which texts changed.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent

#: Leading tag of a text part.
_TAG = re.compile(r"^<([A-Za-z][\w-]*)[\s>/]")
#: What ends a part's identifying opening words: anything that varies per run.
_VARYING = re.compile(r"[/\d`:\"'(\[{<]")


def sha(text: str, n: int = 10) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:n]


def opening(text: str) -> str:
    """The opening words a kind is recognized by: the first line, cut where
    run-specific detail (a path, a number, a quote) starts, at most 40 chars."""
    line = text.lstrip().split("\n", 1)[0]
    m = _VARYING.search(line, 1)
    if m:
        line = line[: m.start()]
    return re.sub(r"\s+", " ", line).strip()[:40]


def raw_kind(text: str) -> str:
    """``<tag>`` for a tagged part (a ``<system-reminder>`` adds its inner
    kind: ``<system-reminder>/<tag>`` or ``<system-reminder>/p:<hash>``),
    else ``p:<hash>`` of the opening words."""
    t = text.lstrip()
    m = _TAG.match(t)
    if not m:
        return "p:" + sha(opening(t), 8)
    tag = m.group(1)
    if tag != "system-reminder":
        return f"<{tag}>"
    inner = t[t.find(">") + 1:].lstrip()
    if inner.startswith("[") and "]" in inner[:60]:
        inner = inner[inner.find("]") + 1:].lstrip()  # a bracketed banner line
    m = _TAG.match(inner)
    return f"<system-reminder>/<{m.group(1)}>" if m else \
        f"<system-reminder>/p:{sha(opening(inner), 8)}"


def load_names(path: Path | None = None) -> dict[str, str]:
    path = path or HERE / "names.json"
    return json.loads(path.read_text("utf-8")) if path.is_file() else {}


#: Tags that start a new part wherever they open a paragraph (other tags,
#: such as a summary's own ``<analysis>``, are content).
PART_TAGS = ("<system-reminder>", "<task-notification>", "<total_tokens>", "<command-name>",
             "<command-message>", "<local-command-stdout>", "<local-command-caveat>")


def split_parts(text: str, starts: set[str],
                patterns: Iterable[re.Pattern[str]] = ()) -> list[str]:
    """A text block cut into parts: a new part begins at a paragraph whose
    raw kind is a known part start (``starts``) or one of :data:`PART_TAGS`,
    or that one of noeta's ``patterns`` names."""
    paras = text.split("\n\n")
    pats = list(patterns)
    parts: list[str] = []
    for p in paras:
        k = raw_kind(p) if p.strip() else ""
        named = any(pat.match(p.strip()) for pat in pats)
        if parts and not (named or k in starts or k.startswith(PART_TAGS)):
            parts[-1] += "\n\n" + p
        else:
            parts.append(p)
    return [p for p in parts if p.strip()] or [text]


def schema_shape(schema: Any) -> Any:
    """A JSON schema with every ``description`` (and ``title``) replaced by its
    length and hash; the rest (types, properties, required, enums, bounds) kept."""
    if isinstance(schema, Mapping):
        out: dict[str, Any] = {}
        for k, v in schema.items():
            if k in ("description", "title") and isinstance(v, str):
                out[k] = {"len": len(v), "sha": sha(v)}
            elif k == "properties" and isinstance(v, Mapping):
                out[k] = {pk: schema_shape(pv) for pk, pv in v.items()}
            else:
                out[k] = schema_shape(v)
        return out
    if isinstance(schema, list):
        return [schema_shape(x) for x in schema]
    return schema


def tool_facts(tool: Mapping[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {"name": tool.get("name")}
    if tool.get("type"):
        out["type"] = tool["type"]
    desc = tool.get("description") or ""
    out["description"] = {"len": len(desc), "sha": sha(desc)}
    out["input_schema"] = schema_shape(tool.get("input_schema") or {})
    for k in ("defer_loading", "cache_control", "strict", "eager_input_streaming"):
        if k in tool:
            out[k] = tool[k]
    extra = sorted(set(tool) - {"name", "type", "description", "input_schema", "defer_loading",
                                "cache_control", "strict", "eager_input_streaming"})
    if extra:
        out["other_keys"] = extra
    return out


def toolset_id(tools: list[dict[str, Any]]) -> str:
    return sha(json.dumps(tools, sort_keys=True), 12)


def _shape(value: Any) -> Any:
    """Keys and value types of a JSON value, not its values."""
    if isinstance(value, Mapping):
        return {k: _shape(v) for k, v in sorted(value.items())}
    if isinstance(value, list):
        return [_shape(v) for v in value[:1]]
    return type(value).__name__


def params(body: Mapping[str, Any]) -> dict[str, Any]:
    """Model-agnostic top-level parameters."""
    out: dict[str, Any] = {"keys": sorted(k for k in body if k not in ("model",))}
    for k in ("max_tokens", "thinking", "context_management", "output_config", "stream",
              "temperature", "top_p", "top_k", "tool_choice", "stop_sequences"):
        if k in body:
            out[k] = body[k]
    oc = out.get("output_config")
    if isinstance(oc, Mapping) and isinstance(oc.get("format"), Mapping):
        fmt = dict(oc["format"])
        if "schema" in fmt:
            fmt["schema"] = schema_shape(fmt["schema"])
        out["output_config"] = {**oc, "format": fmt}
    if "metadata" in body:
        md = body["metadata"]
        shape = _shape(md)
        uid = md.get("user_id") if isinstance(md, Mapping) else None
        if isinstance(uid, str):
            try:
                shape["user_id"] = {"json": _shape(json.loads(uid))}
            except ValueError:
                pass
        out["metadata"] = shape
    if "safeguards" in body:
        out["safeguards"] = [
            {"type": s.get("type"), **({"classifier_context": sorted(s["classifier_context"])}
                                       if isinstance(s.get("classifier_context"), Mapping)
                                       else {})}
            for s in body["safeguards"] if isinstance(s, Mapping)]
    return out


def headers(hdrs: Mapping[str, str]) -> dict[str, Any]:
    low = {k.lower(): v for k, v in hdrs.items()}
    betas = sorted(b.strip() for b in low.get("anthropic-beta", "").split(",") if b.strip())
    return {"betas": betas, "anthropic-version": low.get("anthropic-version"),
            "names": sorted(k for k in low if not k.startswith("x-stainless"))}


def _blocks(content: Any) -> tuple[bool, list[dict[str, Any]]]:
    if isinstance(content, str):
        return True, [{"type": "text", "text": content}]
    return False, list(content or [])


def _cache(block: Mapping[str, Any]) -> str:
    cc = block.get("cache_control")
    if not cc:
        return ""
    return "*" + (cc.get("ttl") or "") if isinstance(cc, Mapping) else "*"


def is_prompt(text: str, prompt: str) -> bool:
    """``text`` is the user's typed ``prompt`` (a recording may carry text
    left in the input box before it)."""
    t = text.strip()
    return t == prompt or (t.endswith(prompt) and len(t) < len(prompt) + 200)


class Kinds:
    """Names text parts: Claude Code's by the shared name table, noeta's by
    the same table plus its own patterns (``noeta_kinds`` in names.json)."""

    def __init__(self, names: Mapping[str, Any] | None = None,
                 prompts: Iterable[str] = (), tasks: Iterable[str] = ()) -> None:
        """``prompts``: what the user typed (kind ``prompt``); ``tasks``: the
        openings of sub-agent task prompts the model wrote (kind ``task``)."""
        names = dict(names if names is not None else load_names())
        self.patterns = [(re.compile(p, re.S), k) for p, k in
                         (names.pop("noeta_kinds", None) or {}).items()]
        self.names: dict[str, str] = {k: v for k, v in names.items() if isinstance(v, str)}
        self.prompts = {p.strip() for p in prompts}
        self.tasks = {t.strip()[:120] for t in tasks if t.strip()}
        self.starts: set[str] = set(self.names)

    def name(self, text: str) -> str:
        t = text.strip()
        if any(is_prompt(t, p) for p in self.prompts):
            return "prompt"
        if any(t.startswith(k) for k in self.tasks):
            return "task"
        for pat, kind in self.patterns:
            if pat.match(t):
                return kind
        raw = raw_kind(text)
        return self.names.get(raw, raw)

    def text_kind(self, text: str) -> str:
        return "+".join(self.name(p) for p in split_parts(
            text, self.starts, (pat for pat, _ in self.patterns)))


def layout(messages: list[Mapping[str, Any]], kinds: Kinds) -> list[str]:
    """One line per message: ``role: tok | tok``, ``role(str)`` when the
    content is a bare string; a token per block: ``text:<kind>`` (assistant
    text is just ``text``), ``tool_use:<name>``, ``tool_result[:error]``,
    ``thinking`` ..., suffixed ``*`` (``*1h``) when it carries a cache mark."""
    out = []
    for m in messages:
        bare, blocks = _blocks(m.get("content"))
        toks = []
        for b in blocks:
            t = b.get("type")
            if t == "text" and m.get("role") == "assistant":
                tok = "text"  # model output: no kind
            elif t == "text":
                tok = "text:" + kinds.text_kind(b.get("text") or "")
            elif t == "tool_use":
                tok = "tool_use:" + str(b.get("name"))
            elif t == "tool_result":
                tok = "tool_result" + (":error" if b.get("is_error") else "")
                inner = b.get("content")
                if isinstance(inner, list):
                    tok += "[" + ",".join(str(x.get("type")) for x in inner) + "]"
            else:
                tok = str(t)
            toks.append(tok + _cache(b))
        out.append(f"{m.get('role')}{'(str)' if bare else ''}: " + " | ".join(toks))
    return out


def system_facts(system: Any, kinds: Kinds) -> list[dict[str, Any]]:
    if isinstance(system, str):
        system = [{"type": "text", "text": system}]
    out = []
    for s in system or []:
        text = s.get("text") or ""
        row = {"kind": kinds.name(text), "len": len(text), "sha": sha(text)}
        if s.get("cache_control"):
            row["cache"] = _cache(s)
        out.append(row)
    return out


#: A tool name on its own line (the deferred-tool list names one per line).
_TOOL_NAME = re.compile(r"^[A-Za-z_][\w-]*$")


def deferred_names(messages: Iterable[Mapping[str, Any]], kinds: Kinds) -> list[str]:
    """The tool names the request's deferred-tool lists name (wire names,
    not text), sorted; empty when no message carries one."""
    names: set[str] = set()
    for m in messages:
        if m.get("role") == "assistant":
            continue
        _, blocks = _blocks(m.get("content"))
        for b in blocks:
            if b.get("type") != "text":
                continue
            for part in split_parts(b.get("text") or "", kinds.starts,
                                    (pat for pat, _ in kinds.patterns)):
                if kinds.name(part) != "deferred-tools":
                    continue
                lines = part.strip().split("\n")[1:]
                names.update(x.strip() for x in lines if _TOOL_NAME.match(x.strip()))
    return sorted(names)


def request_facts(body: Mapping[str, Any], hdrs: Mapping[str, str], kinds: Kinds,
                  toolsets: dict[str, list[dict[str, Any]]]) -> dict[str, Any]:
    """The facts of one request; its tool list goes into ``toolsets`` by id."""
    tools = [tool_facts(t) for t in body.get("tools") or []]
    tid = toolset_id(tools) if tools else None
    if tid:
        toolsets.setdefault(tid, tools)
    messages = list(body.get("messages") or [])
    out = {
        "params": params(body),
        "headers": headers(hdrs),
        "system": system_facts(body.get("system"), kinds),
        "toolset": tid,
        "messages": layout(messages, kinds),
    }
    deferred = deferred_names(messages, kinds)
    if deferred:
        out["deferred"] = deferred
    return out


def text_catalog(body: Mapping[str, Any], kinds: Kinds, into: dict[str, dict[str, int | str]]
                 ) -> None:
    """Record the hash and length of every named part's full text by kind, so
    a later recording shows which texts changed."""
    def add(text: str) -> None:
        for part in split_parts(text, kinds.starts):
            k = kinds.name(part)
            if k in ("prompt",):
                continue
            into.setdefault(k, {}).setdefault(sha(part), len(part))  # type: ignore[arg-type]

    sysb = body.get("system")
    for s in ([{"text": sysb}] if isinstance(sysb, str) else sysb or []):
        add(s.get("text") or "")
    for m in body.get("messages") or []:
        _, blocks = _blocks(m.get("content"))
        for b in blocks:
            if b.get("type") == "text" and m.get("role") != "assistant":
                add(b.get("text") or "")
