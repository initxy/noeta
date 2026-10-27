"""Model-facing texts of delegation, copied from 0.6 byte for byte (file and
line of origin noted on each)."""

from __future__ import annotations

from functools import cache
from importlib import resources

#: The delegation tool's model-visible name (0.6 ``SPAWN_SUBAGENT_TOOL``).
TASK_TOOL = "Task"


@cache
def load_description(name: str) -> str:
    """``descriptions/<name>.md`` without its trailing newline (0.6's
    ``load_markdown`` default)."""
    text = (resources.files("noeta.ext.subtasks").joinpath("descriptions", f"{name}.md")
            .read_text(encoding="utf-8"))
    return text.rstrip("\n")


@cache
def load_prompt(name: str) -> str:
    """``prompts/<name>.md`` byte for byte (prompts keep their newline)."""
    return (resources.files("noeta.ext.subtasks").joinpath("prompts", f"{name}.md")
            .read_text(encoding="utf-8"))


# 0.6 builtins/delegation/impl/__init__.py:78-108 (schema property texts).
DESCRIPTION_PROP = "A short (3-5 word) description of the task."
PROMPT_PROP = "The task for the agent to perform."
SUBAGENT_TYPE_PROP = "The type of specialized agent to use for this task."

# 0.6 builtins/delegation/impl/__init__.py:135-155 (argument refusals) and
# :196-236 (whole-response refusals).
NOTHING_RAN = "Nothing in this response ran: {reason}"
BAD_BACKGROUND = (
    "'background' must be true or false (a JSON boolean), got "
    "{value!r}. Re-issue the call with a boolean or without it."
)
EMPTY_PROMPT = (
    "'prompt' is empty. Re-issue the call with the task the agent "
    "should perform."
)
MISSING_FIELDS = (
    "Nothing in this response ran: every Task call needs "
    "string 'subagent_type' and 'prompt' arguments. Re-issue "
    "them with both set."
)
EXCLUSIVE = (
    "Nothing in this response ran: Task cannot share a response "
    "with {name}. Re-issue the other calls in a separate response."
)

# 0.6 core/_decision_handlers.py:1555-1559.
SPAWN_DENIED = (
    "Delegation refused: {reason}. No sub-agent started; none of this "
    "response's Task calls ran. Use an offered sub-agent name, or do the work "
    "yourself."
)
# 0.6 builtins/governance/impl/permission.py:192-195.
UNKNOWN_AGENT = "no sub-agent named {name!r} (available: {available})"
# 0.6 builtins/governance/impl/budget.py:85-87.
DEPTH_REACHED = "max_subtask_depth={n} reached"

# 0.6 core/_decision_handlers.py:2136-2264.
MAX_FANOUT = 16
FANOUT_TOO_BIG = (
    "{n} Task calls in one response exceeds the limit of "
    "{limit}. None were started — re-issue them in batches "
    "of {limit} or fewer."
)
DUPLICATE_CALL_ID = (
    "Two Task calls in this response share one call id, so their "
    "results could not be told apart. None were started — "
    "re-issue them as separate calls with distinct ids."
)

# 0.6 core/engine.py:510 (a failed child's result with no error of its own).
SUBAGENT_FAILED = "sub-agent failed"

# 0.6 core/_decision_handlers.py:1925-1945 (background launch ack).
BACKGROUND_STARTED = (
    'Background sub-agent "{agent}" started '
    "(id {id}). It runs concurrently while you keep working; "
    "its actual result will be delivered to you automatically when it "
    "finishes — you do NOT need to wait or poll for it. "
    "IMPORTANT: Do NOT guess, invent, or fabricate this sub-agent's "
    "result in any output you produce before you receive the real "
    "completion notice. If you need its result to answer the user, "
    "say so explicitly and move on to other work in the meantime."
)
# 0.6 execution/background_subagent.py:116-127 and _decision_handlers.py:1899.
MAX_BACKGROUND = 8
BACKGROUND_NOT_STARTED = "background sub-agent not started: {reason}"
TOO_MANY_BACKGROUND = (
    "too many background sub-agents "
    "({running}/{limit}) running for this session; "
    "let one finish before starting another"
)

# 0.6 client/host.py:1571, 1583-1592 and execution/driver.py:482-486.
BACKGROUND_FINISHED = 'Background sub-agent "{agent}" finished. Here is its result:'
BACKGROUND_FAILED = 'Background sub-agent "{agent}" did not complete.'
BACKGROUND_NEEDED_HUMAN = (
    "the sub-agent stopped without completing (it needed an interaction "
    "that is unavailable to a background agent)"
)
BACKGROUND_NOTICE = (
    "{summary}\n\n"
    "{result}\n\n"
    '<background-subagent id="{id}" status="{status}"/>'
)
