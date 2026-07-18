"""Template validation and placeholder utilities.

A single-node template = name + description + prompt (with ``{param}``
placeholders) + a list of param definitions. Placeholder syntax: ``{param
name}`` is replaced as a whole; a param name contains no braces and no
newlines. Validation policy:

- **Hard errors** (save rejected): empty name / prompt, empty or duplicate
  param names, malformed structure.
- **Soft warnings** (saved, returned with the response): a placeholder in
  the prompt has no matching param definition, or a param definition never
  appears in the prompt — usually a typo, but not blocking (the author may
  mean it).
"""
from __future__ import annotations

import re
from typing import Any

_PLACEHOLDER_RE = re.compile(r"\{([^{}\n]+)\}")

NAME_MAX_LEN = 64
DESC_MAX_LEN = 500
PROMPT_MAX_LEN = 32000
PARAM_NAME_MAX_LEN = 64
PARAM_DESC_MAX_LEN = 500
MAX_PARAMS = 20
MAX_NODES = 10


class TemplateValidationError(ValueError):
    """Malformed template structure (the API layer maps it to 422)."""


def extract_placeholders(prompt: str) -> list[str]:
    """List of placeholder names appearing in the prompt (deduplicated,
    keeping first-appearance order)."""
    seen: set[str] = set()
    out: list[str] = []
    for m in _PLACEHOLDER_RE.finditer(prompt or ""):
        name = m.group(1).strip()
        if name and name not in seen:
            seen.add(name)
            out.append(name)
    return out


def render_prompt(prompt: str, values: dict[str, str]) -> str:
    """Replace ``{param}`` placeholders in the prompt with param values;
    placeholders without a value are kept verbatim."""
    def _sub(m: re.Match) -> str:
        name = m.group(1).strip()
        val = values.get(name)
        return val if isinstance(val, str) and val else m.group(0)

    return _PLACEHOLDER_RE.sub(_sub, prompt or "")


def normalize_params(params: Any) -> list[dict]:
    """Validate and normalize the param definition list; raises
    :class:`TemplateValidationError` when malformed."""
    if params is None:
        return []
    if not isinstance(params, list):
        raise TemplateValidationError("params must be a list")
    if len(params) > MAX_PARAMS:
        raise TemplateValidationError(f"at most {MAX_PARAMS} params")
    out: list[dict] = []
    seen: set[str] = set()
    for i, p in enumerate(params):
        if not isinstance(p, dict):
            raise TemplateValidationError(f"param #{i + 1} must be an object")
        name = str(p.get("name") or "").strip()
        if not name:
            raise TemplateValidationError(f"param #{i + 1} is missing a name")
        if len(name) > PARAM_NAME_MAX_LEN:
            raise TemplateValidationError(f"param name too long: {name[:20]}…")
        if "{" in name or "}" in name or "\n" in name:
            raise TemplateValidationError(f"param name contains illegal characters: {name}")
        if name in seen:
            raise TemplateValidationError(f"duplicate param name: {name}")
        seen.add(name)
        desc = str(p.get("description") or "").strip()[:PARAM_DESC_MAX_LEN]
        out.append({
            "name": name,
            "description": desc,
            "required": bool(p.get("required", False)),
        })
    return out


def validate_template_fields(name: str, prompt: str) -> None:
    """Hard validation for a single-node template; raises
    :class:`TemplateValidationError` when malformed."""
    if not (name or "").strip():
        raise TemplateValidationError("template name must not be empty")
    if len(name) > NAME_MAX_LEN:
        raise TemplateValidationError(
            f"template name is at most {NAME_MAX_LEN} characters"
        )
    if not (prompt or "").strip():
        raise TemplateValidationError("prompt must not be empty")
    if len(prompt) > PROMPT_MAX_LEN:
        raise TemplateValidationError(
            f"prompt is at most {PROMPT_MAX_LEN} characters"
        )


def placeholder_warnings(prompt: str, params: list[dict]) -> list[str]:
    """Soft consistency warnings between placeholders and param definitions
    (do not block saving)."""
    placeholders = set(extract_placeholders(prompt))
    param_names = {p["name"] for p in params}
    warnings: list[str] = []
    for name in sorted(placeholders - param_names):
        warnings.append(
            f"placeholder {{{name}}} in the prompt has no matching param definition"
        )
    for name in sorted(param_names - placeholders):
        warnings.append(f'param "{name}" is not used in the prompt')
    return warnings


def normalize_workflow_nodes(nodes: Any) -> list[dict]:
    """Validate and normalize the workflow node list (keeping only
    template_id); raises when malformed."""
    if not isinstance(nodes, list) or not nodes:
        raise TemplateValidationError("a workflow needs at least one node")
    if len(nodes) > MAX_NODES:
        raise TemplateValidationError(f"at most {MAX_NODES} nodes")
    out: list[dict] = []
    for i, n in enumerate(nodes):
        if not isinstance(n, dict):
            raise TemplateValidationError(f"node #{i + 1} must be an object")
        tid = str(n.get("template_id") or "").strip()
        if not tid:
            raise TemplateValidationError(f"node #{i + 1} is missing template_id")
        out.append({"template_id": tid})
    return out
