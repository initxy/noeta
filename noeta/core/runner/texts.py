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
#: A child named onto a queue this worker does not serve while it holds the
#: tree (for a background job): no worker could run the child until then.
UNSERVED_QUEUE_ERROR = (
    "cannot start an agent on queue '{queue}' now: this worker holds the task tree for a "
    "background command and does not serve that queue. Start it after the background "
    "command ends, or on this worker's queue.")
DENIED_BY_HUMAN = "denied by human"
#: A call the person declined at its approval prompt; the turn then stops.
REJECTED_BY_USER = (
    "The user declined this tool call, so it did not run (for a file edit: nothing was "
    "written). Stop here and wait for the user to tell you how to go on.")
#: What the person said when declining, after the text above.
REJECTED_WITH_MESSAGE = "\nThe user's reason for declining: {message}"
#: Follows the results of a reply in which the person declined a call
#: (Claude Code's marker, verbatim: models know it).
REJECTED_MARKER = "[Request interrupted by user for tool use]\n"
#: Opens the person's next turn after they stopped a reply mid-stream, after
#: the part of the reply that streamed (Claude Code's marker, verbatim).
INTERRUPTED_MARKER = "[Request interrupted by user]\n"
#: A call a permission callback refused without stopping the turn (an
#: ``Answer`` whose data says ``"interrupt": false``) and gave no reason for.
DENIED_BY_CALLBACK = "Permission to use {name} was denied."
UNKNOWN_TOOL = "No tool named {name!r} is available in this task; use one of the offered tools."

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
#: Asked once per step when a reply ends with no text and no tool call
#: (Claude Code 2.1.312's text).
EMPTY_REPLY_NUDGE = (
    "[Your previous response had no visible output. Please continue and "
    "produce a user-visible response.]"
)

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
#: The turn reached ``config["max_turns"]`` model calls (Claude Code's
#: ``--max-turns``); the detail says how many.
MAX_TURNS = "max_turns"
MAX_TURNS_DETAIL = "Reached maximum number of turns ({n})"
#: A bug in the runner, an extension hook or a provider adapter.
INTERNAL_ERROR = "internal_error"
#: A task whose log no claim could read or fold (``LOAD_ATTEMPTS`` in a row).
UNREADABLE_LOG = ("unreadable_log: the task's log could not be read on {attempts} claims in "
                  "a row; last error: {error}")
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

#: A task that started with plugins this worker does not have (host-facing).
PLUGIN_MISSING = ("plugin_missing: this task runs with {names}, which the worker that claimed "
                  "it does not have; install them there (or run the task where they are)")
