"""Running one hook: a command on this machine, an HTTP POST, or a question
to the model. Each comes back as a :class:`HookResult` read the same way:
exit code 0 with stdout (plain text or Claude Code's JSON object), exit
code 2 with stderr as the reason to block, anything else a failure that is
logged and otherwise ignored."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import re
import signal
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any

import httpx

from noeta.core.execenv import scrub_env
from noeta.core.types import Message, ModelRequest, ModelResponse, SystemPart, Text, user
from noeta.plugins.hooks.config import HookCommand

log = logging.getLogger("noeta.hooks")

_OUTPUT_CAP = 64 * 1024
#: Exit code of a hook that could not run or did not answer in time.
FAILED = -1


@dataclass(frozen=True)
class HookResult:
    exit_code: int
    stdout: str = ""
    stderr: str = ""

    @property
    def data(self) -> dict[str, Any] | None:
        """Stdout as a JSON object, when it is one (exit 0 only)."""
        text = self.stdout.strip()
        if self.exit_code != 0 or not text.startswith("{"):
            return None
        try:
            value = json.loads(text)
        except ValueError:
            return None
        return value if isinstance(value, dict) else None

    @property
    def text(self) -> str:
        """Plain stdout (not a JSON object) of a successful run."""
        return self.stdout.strip() if self.exit_code == 0 and self.data is None else ""

    @property
    def failed(self) -> bool:
        return self.exit_code not in (0, 2)


Runner = Callable[[HookCommand, dict[str, Any], "str | None"], Awaitable[HookResult]]
Complete = Callable[[ModelRequest], Awaitable[ModelResponse]]


def _project_vars(text: str, cwd: str | None) -> str:
    if not cwd:
        return text
    return re.sub(r"\$\{(CLAUDE|NOETA)_PROJECT_DIR\}", lambda _m: cwd, text)


async def run_command(hook: HookCommand, payload: dict[str, Any], cwd: str | None) -> HookResult:
    """Run a command hook: ``sh -c command`` (or, with ``args``, the
    program ``command`` with those arguments and no shell), the payload as
    JSON on stdin, a scrubbed environment plus ``NOETA_PROJECT_DIR`` and
    ``CLAUDE_PROJECT_DIR``; killed after its timeout."""
    env = scrub_env()
    if cwd:
        env["NOETA_PROJECT_DIR"] = env["CLAUDE_PROJECT_DIR"] = cwd
    argv = [_project_vars(hook.command, cwd), *(_project_vars(a, cwd) for a in hook.args)] \
        if hook.args is not None else ["sh", "-c", hook.command]
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv, stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            cwd=cwd if cwd and os.path.isdir(cwd) else None, env=env, start_new_session=True)
    except OSError as exc:
        return HookResult(exit_code=FAILED, stderr=str(exc))
    try:
        out, err = await asyncio.wait_for(
            proc.communicate(json.dumps(payload).encode()), hook.timeout)
    except TimeoutError:
        # The whole group: a shell's children hold the pipes open otherwise.
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(proc.pid, signal.SIGKILL)
        await proc.wait()
        return HookResult(exit_code=FAILED, stderr=f"timed out after {hook.timeout:g}s")
    return HookResult(exit_code=proc.returncode or 0,
                      stdout=out[:_OUTPUT_CAP].decode("utf-8", "replace"),
                      stderr=err[:_OUTPUT_CAP].decode("utf-8", "replace"))


_ENV_REF = re.compile(r"\$\{([A-Z_][A-Z0-9_]*)\}|\$([A-Z_][A-Z0-9_]*)")


def header_value(value: str, allowed: tuple[str, ...],
                 environ: Mapping[str, str] | None = None) -> str:
    """A header value with ``$NAME`` / ``${NAME}`` replaced by the
    environment variable, for names in ``allowed`` only (any other becomes
    empty); line breaks are dropped."""
    env = os.environ if environ is None else environ

    def sub(m: re.Match[str]) -> str:
        name = m.group(1) or m.group(2)
        if name not in allowed:
            log.warning("hooks: $%s is not in allowedEnvVars; left empty", name)
            return ""
        return env.get(name, "")

    return re.sub(r"[\r\n\x00]", "", _ENV_REF.sub(sub, value))


async def run_http(hook: HookCommand, payload: dict[str, Any],
                   transport: httpx.AsyncBaseTransport | None = None) -> HookResult:
    """POST the payload as JSON to the hook's URL (no redirects). A 2xx
    answer's body is read like a command's stdout and must be a JSON object
    (empty counts as ``{}``); anything else is a failure."""
    headers = {"Content-Type": "application/json"}
    for name, value in hook.headers:
        headers[name] = header_value(value, hook.allowed_env_vars)
    try:
        async with httpx.AsyncClient(transport=transport, follow_redirects=False,
                                     timeout=hook.timeout) as client:
            response = await client.post(hook.url, content=json.dumps(payload).encode(),
                                         headers=headers)
    except httpx.TimeoutException:
        return HookResult(exit_code=FAILED, stderr=f"timed out after {hook.timeout:g}s")
    except httpx.HTTPError as exc:
        return HookResult(exit_code=FAILED, stderr=f"{type(exc).__name__}: {exc}")
    body = response.text[:_OUTPUT_CAP]
    if not 200 <= response.status_code < 300:
        return HookResult(exit_code=FAILED, stderr=f"HTTP {response.status_code}: {body[:500]}")
    text = body.strip() or "{}"
    if not text.startswith("{"):
        return HookResult(exit_code=FAILED, stderr=f"the response is not a JSON object: "
                                                   f"{text[:200]}")
    return HookResult(exit_code=0, stdout=text)


#: What a prompt hook's model is told; the hook's own text is the question.
PROMPT_SYSTEM = (
    "You check one condition for an automated agent. The user message states the "
    "condition and the data it applies to. Answer with a single JSON object and nothing "
    'else: {"ok": true, "reason": "..."} when the condition holds and the action may go '
    'ahead, {"ok": false, "reason": "..."} when it does not. Always give a short reason.'
)
PROMPT_STOP_SYSTEM = (
    "You check whether an automated agent may stop. The conversation so far comes first; "
    "the last user message states the condition. Judge from the conversation only. Answer "
    'with a single JSON object and nothing else: {"ok": true, "reason": "..."} when the '
    'condition is met, {"ok": false, "reason": "..."} when it is not (say what is missing). '
    'When the condition can never be met in this conversation, answer {"ok": false, '
    '"impossible": true, "reason": "..."}.'
)
_STOP_QUESTION = "Has this stopping condition been met?\nCondition: {prompt}"


def _verdict(text: str) -> dict[str, Any] | None:
    text = text.strip()
    if text.startswith("```"):
        text = text.strip("`").removeprefix("json").strip()
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end < start:
        return None
    try:
        value = json.loads(text[start:end + 1])
    except ValueError:
        return None
    return value if isinstance(value, dict) and isinstance(value.get("ok"), bool) else None


async def run_prompt(hook: HookCommand, payload: dict[str, Any], complete: Complete | None,
                     model: str, transcript: list[Message] | None = None) -> HookResult:
    """Ask the model whether the hook's condition holds (``$ARGUMENTS`` in
    the prompt is replaced by the payload as JSON). ``ok`` passes; not
    ``ok`` blocks with the model's reason (and, unless the hook says
    ``continueOnBlock``, stops the turn), except on Stop events, where it
    only keeps the agent going; a condition judged impossible lets it stop.
    ``transcript`` (Stop events) goes before the question."""
    if complete is None:
        return HookResult(exit_code=FAILED, stderr="prompt hooks need a model here")
    arguments = json.dumps(payload)
    text = hook.prompt.replace("$ARGUMENTS", arguments) if "$ARGUMENTS" in hook.prompt \
        else f"{hook.prompt}\n\n{arguments}"
    stop = transcript is not None
    question = _STOP_QUESTION.format(prompt=text) if stop else text
    request = ModelRequest(
        model=hook.model or model,
        system=[SystemPart(text=PROMPT_STOP_SYSTEM if stop else PROMPT_SYSTEM)],
        messages=[*(transcript or []), user(question)], thinking=False, max_tokens=1024)
    try:
        response = await asyncio.wait_for(complete(request), hook.timeout)
    except TimeoutError:
        return HookResult(exit_code=FAILED, stderr=f"timed out after {hook.timeout:g}s")
    except Exception as exc:  # noqa: BLE001 — a failing evaluation is a hook failure
        return HookResult(exit_code=FAILED, stderr=f"{type(exc).__name__}: {exc}")
    reply = "".join(b.text for b in response.message.content if isinstance(b, Text))
    verdict = _verdict(reply)
    if verdict is None:
        return HookResult(exit_code=FAILED, stderr=f"the model did not answer with "
                                                   f"{{\"ok\": ...}}: {reply[:300]}")
    reason = str(verdict.get("reason") or "")
    if verdict["ok"]:
        return HookResult(exit_code=0, stdout="{}")
    if stop and verdict.get("impossible") is True:
        return HookResult(exit_code=0, stdout=json.dumps({"systemMessage": reason}))
    out: dict[str, Any] = {"decision": "block", "reason": reason or "the condition was not met"}
    if not stop and not hook.continue_on_block:
        out["continue"] = False
        out["stopReason"] = out["reason"]
    return HookResult(exit_code=0, stdout=json.dumps(out))
