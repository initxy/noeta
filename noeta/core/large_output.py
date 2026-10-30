"""The large-output rule (Claude Code's): an output over a limit is saved
whole to a file the model can ``Read`` and replaced by a preview. Tools apply
it to their own output; the runner applies a ceiling per tool and a budget per
reply to every result.

A task's saved outputs live in its own directory below the spill root,
written through the task's exec env: wherever the task runs next (another
worker, after a crash or a wait), the model reads them through that env.
"""

from __future__ import annotations

import hashlib
import posixpath
import re
from dataclasses import dataclass

from noeta.core.execenv import ExecEnv
from noeta.core.tool import (
    REPLY_RESULTS_BUDGET_CHARS,
    RESULT_CEILING_CHARS,
    Tool,
    ToolContext,
    ToolOutput,
)
from noeta.core.types import Text

__all__ = [
    "PERSISTED_PREVIEW_CHARS",
    "SPILL_MARKER",
    "ReplyBudget",
    "bound_result",
    "file_name_part",
    "format_size",
    "large_output_message",
    "limit_output",
    "mark_spill_dir",
    "result_spill_path",
    "save_spilled",
    "spill_path",
    "task_spill_dir",
]


def file_name_part(text: str, default: str) -> str:
    """``text`` as one file name: characters other than letters, digits,
    ``_``, ``.`` and ``-`` replaced by ``_``; ``default`` when that leaves
    no name or ``.`` / ``..``. For names built from ids a provider or
    client picked."""
    name = re.sub(r"[^A-Za-z0-9_.-]", "_", text)
    return default if name in ("", ".", "..") else name


def format_size(n: int) -> str:
    """Claude Code's size label: ``512 bytes``, ``48.8KB``, ``1.2MB``."""
    kb = n / 1024
    if kb < 1:
        return f"{n} bytes"
    if kb < 1024:
        return f"{kb:.1f}".removesuffix(".0") + "KB"
    mb = kb / 1024
    if mb < 1024:
        return f"{mb:.1f}".removesuffix(".0") + "MB"
    return f"{mb / 1024:.1f}".removesuffix(".0") + "GB"


#: Characters of a spilled output shown inline as its preview.
PERSISTED_PREVIEW_CHARS = 2000


def _preview(text: str, limit: int) -> tuple[str, bool]:
    """The first ``limit`` characters of ``text``, ended at the last newline
    when that keeps more than half, and whether anything was left out."""
    if len(text) <= limit:
        return text, False
    newline = text[:limit].rfind("\n")
    end = newline if newline > limit * 0.5 else limit
    return text[:end], True


def _tail(text: str, limit: int) -> str:
    """The last ``limit`` characters of ``text``, started after the first
    newline when that keeps more than half."""
    if len(text) <= limit:
        return text
    if limit <= 0:
        return ""
    start = len(text) - limit
    newline = text.find("\n", start)
    if newline != -1 and len(text) - (newline + 1) > limit * 0.5:
        return text[newline + 1 :]
    return text[start:]


def large_output_message(output: str, limit: int, *, saved_to: str | None = None) -> str:
    """What the model sees for an output over ``limit`` — Claude Code's
    wording. With ``saved_to`` a ``<persisted-output>`` block (size, path, a
    2 KB preview); without, a ``<truncated-output>`` block keeping as much of
    the start and the end as fits ``limit``."""
    size = format_size(len(output))
    if saved_to is not None:
        preview, has_more = _preview(output, PERSISTED_PREVIEW_CHARS)
        return (
            "<persisted-output>\n"
            f"Output too large ({size}). Full output saved to: {saved_to}\n\n"
            f"Preview (first {format_size(PERSISTED_PREVIEW_CHARS)}):\n"
            f"{preview}" + ("\n...\n" if has_more else "\n")
            + "</persisted-output>"
        )

    def truncated(first: str, last: str, body: str) -> str:
        return (
            "<truncated-output>\n"
            f"Output too large ({size}). It could not be saved, so only the "
            f"first {first} and the last {last} are shown; the middle was "
            "dropped. If the tool can page or filter its results, call it "
            f"again for the part you need.\n{body}\n</truncated-output>"
        )

    def elision(dropped: int) -> str:
        return f"\n... [{dropped} characters dropped] ...\n"

    # Sized against the longest labels the shown parts could get, so the
    # whole block stays within ``limit``.
    widest = "1023 bytes"
    room = max(0, limit - len(truncated(widest, widest, "")) - len(elision(len(output))))
    head, _ = _preview(output, room // 2)
    tail = _tail(output[len(head) :], room - len(head))
    dropped = len(output) - len(head) - len(tail)
    return truncated(
        format_size(len(head)), format_size(len(tail)), head + elision(dropped) + tail
    )


#: The file in a task's spill directory naming the task it belongs to (its
#: directory name cannot always be read back as the id).
SPILL_MARKER = ".task-id"


def task_spill_dir(spill_dir: str, task_id: str) -> str:
    """A task's directory below the spill root: its id when that is a safe
    file name, else the id made safe plus a short hash of it, so two ids
    never share a directory."""
    part = file_name_part(task_id, "task")
    if part != task_id:
        part += "-" + hashlib.sha256(task_id.encode("utf-8")).hexdigest()[:10]
    return posixpath.join(spill_dir, part)


async def mark_spill_dir(env: ExecEnv, directory: str, task_id: str) -> None:
    """Write :data:`SPILL_MARKER` into a task's spill directory once (what
    the host's sweep goes by); a failure leaves it unmarked, never swept."""
    marker = posixpath.join(directory, SPILL_MARKER)
    try:
        if await env.stat(marker) is None:
            await env.write_bytes(marker, task_id.encode("utf-8"))
    except OSError:
        pass


def spill_path(spill_dir: str, task_id: str, call_id: str) -> str:
    """Where one call's full output is saved: a pure function of the
    directory, the task and the call, so a re-run names the same file."""
    call_part = file_name_part(call_id, "call")
    return posixpath.join(task_spill_dir(spill_dir, task_id), f"{call_part}.txt")


async def save_spilled(ctx: ToolContext, path: str, data: bytes) -> bool:
    """Write ``data`` at ``path`` (in the task's spill directory) through the
    task's env, marking the directory as the task's; ``False`` on failure."""
    try:
        await ctx.env.write_bytes(path, data)
    except OSError:
        return False
    if ctx.spill_dir:
        await mark_spill_dir(ctx.env, task_spill_dir(ctx.spill_dir, ctx.task_id), ctx.task_id)
    return True


def result_spill_path(spill_dir: str, task_id: str, call_id: str) -> str:
    """Where the runner saves one call's whole result: beside the call's own
    file (:func:`spill_path`, a tool's), never over it."""
    return spill_path(spill_dir, task_id, call_id).removesuffix(".txt") + ".result.txt"


def _persisted(text: str) -> bool:
    """Whether ``text`` already points at its saved whole (a tool saved its
    own output, :func:`limit_output`)."""
    return "<persisted-output>\nOutput too large (" in text and "Full output saved to: " in text


async def _save(text: str, path: str, ctx: ToolContext) -> bool:
    return await save_spilled(ctx, path, text.encode("utf-8"))


async def limit_output(text: str, limit: int, ctx: ToolContext) -> str:
    """``text`` unchanged when it fits ``limit`` characters. Past it, the
    whole text is saved under ``ctx.spill_dir`` (through the env, so the
    model can ``Read`` it) and a preview is returned; with no spill directory,
    or when the save fails, the start and end are kept around an elision."""
    if len(text) <= limit:
        return text
    saved_to: str | None = None
    if ctx.spill_dir:
        path = spill_path(ctx.spill_dir, ctx.task_id, ctx.call_id)
        saved_to = path if await _save(text, path, ctx) else None
    return large_output_message(text, limit, saved_to=saved_to)


@dataclass
class ReplyBudget:
    """Characters of text the results of one reply's calls hold so far,
    shared by calls that run together (each reserves its share before its
    first wait, so calls finishing together cannot all pass the budget)."""

    used: int = 0


async def bound_result(output: ToolOutput, tool: Tool, ctx: ToolContext,
                       budget: ReplyBudget) -> ToolOutput:
    """``output`` within Claude Code's result limits: a text result over the
    tool's ``max_result_chars`` (at most ``RESULT_CEILING_CHARS``) is saved
    and previewed; one that would take the reply's results past
    ``REPLY_RESULTS_BUDGET_CHARS`` is saved too when there is a spill
    directory. The runner's file is its own (:func:`result_spill_path`), and
    a result that already points at its saved whole is never saved again.
    When the save fails the result stays whole, as Claude Code keeps it;
    with no spill directory one over the ceiling keeps its start and end.
    Errors and results with images pass unchanged."""
    if output.is_error or output.park is not None or not output.content \
            or not all(isinstance(b, Text) for b in output.content):
        return output
    text = "".join(b.text for b in output.content if isinstance(b, Text))
    declared = getattr(tool, "max_result_chars", RESULT_CEILING_CHARS)
    limit = len(text) if declared is None else min(declared, RESULT_CEILING_CHARS)
    if len(text) <= limit and budget.used + len(text) > REPLY_RESULTS_BUDGET_CHARS \
            and len(text) > PERSISTED_PREVIEW_CHARS and ctx.spill_dir and declared is not None \
            and not _persisted(text):
        limit = PERSISTED_PREVIEW_CHARS  # past the reply's budget: saved whole
    if len(text) <= limit:
        budget.used += len(text)
        return output
    # Reserved before the first wait, so calls finishing together cannot
    # all pass the budget; settled to what is returned once it is known.
    reserved = min(len(text), limit + PERSISTED_PREVIEW_CHARS)
    budget.used += reserved
    if not ctx.spill_dir:
        bounded = large_output_message(text, limit)
    else:
        path = result_spill_path(ctx.spill_dir, ctx.task_id, ctx.call_id)
        if not await _save(text, path, ctx):
            budget.used += len(text) - reserved
            return output
        bounded = large_output_message(text, limit, saved_to=path)
    budget.used += len(bounded) - reserved
    return ToolOutput(content=[Text(text=bounded)], meta={**output.meta, "persisted": True},
                      display=output.display, end_turn=output.end_turn,
                      block_list=output.block_list)
