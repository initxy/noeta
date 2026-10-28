"""Model-facing and host-facing texts of the core loop, kept from 0.6 (and
Claude Code where noted) byte for byte."""

INTERRUPTED_CALL_ERROR = (
    "Interrupted before its result was recorded, so its outcome is unknown — "
    "it may have fully, partly, or not applied. Check its effect before "
    "running it again."
)
STOPPED_CALL_ERROR = "interrupted: a human stop landed during this batch"
#: Answers a call left open when its turn failed (followed by the reason).
TURN_FAILED_CALL_ERROR = "not run: the turn failed: "
DENIED_BY_HUMAN = "denied by human"
UNKNOWN_TOOL = "No tool named {name!r} is available in this task; use one of the offered tools."
DEFERRED_NOT_LOADED = (
    "InputValidationError: {name} is a deferred tool whose schema is not loaded. Load it with "
    "ToolSearch (query \"select:{name}\"), then call it again.")

#: Output-limit recovery, copied from Claude Code (2.1.300).
OUTPUT_LIMIT_RESUME_NUDGE = (
    "Output token limit hit. Resume directly — no apology, no recap of "
    "what you were doing. Pick up mid-thought if that is where the cut "
    "happened. Break remaining work into smaller pieces."
)
OUTPUT_LIMIT_THINKING_NUDGE = (
    "Output token limit hit while you were still reasoning, before this "
    "response produced any text or tool call. That reasoning was not kept and "
    "is not part of this conversation, so there is nothing to resume. "
    "Continue from the conversation above: reason much more briefly and "
    "produce your answer or next tool call well within the output limit."
)
#: Consecutive output-cap recoveries before the turn fails (Claude Code's limit).
MAX_OUTPUT_TOKENS_RECOVERY = 3

#: The trailing user turn of the step-limit round (not recorded).
STEP_LIMIT_PROMPT = (
    "You have reached the limit of {n} steps for this task, and no more tool "
    "calls will run. Stop here and reply with your final report: what you "
    "did, what you found, where the work stands, and what is left to do."
)
#: Opens the answer a step-limit stop finishes with.
STEP_LIMIT_HEAD = "Stopped at the {n}-step limit before finishing."

#: Turn-failure reasons hosts branch on (``Waiting.detail["turn_failed"]``
#: and ``TaskEnded.error`` start with one of these).
LLM_ERROR = "llm_error"
LLM_EMPTY_RESPONSE = "llm_empty_response"
LLM_TRUNCATED = "llm_truncated"
CONTEXT_OVERFLOW = "context_overflow"
#: A bug in the runner, an extension hook or a provider adapter.
INTERNAL_ERROR = "internal_error"
#: An unattended task (``config["unattended"]``, e.g. a background sub-agent)
#: reached an approval or a question nobody can answer.
NEEDS_HUMAN = "needs_human"
NEEDS_HUMAN_DETAIL = (
    "the sub-agent stopped without completing (it needed an interaction "
    "that is unavailable to a background agent)"
)

#: Error bodies quoted in a failure detail are cut to this many characters.
ERROR_DETAIL_CHARS = 500
#: Concurrency-safe calls of one reply that run at once (0.6's cap).
MAX_TOOL_USE_CONCURRENCY = 10
#: A model call started this many times without a recorded reply (crashes
#: mid-call) fails the task instead of looping.
MAX_MODEL_ATTEMPTS = 3
