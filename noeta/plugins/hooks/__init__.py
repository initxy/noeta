"""Claude Code's hooks: commands, HTTP endpoints or model checks a person
configures to run at fixed points of a task; they can add context for the
model, change or stop what is about to happen, or answer for the person.

The configuration is Claude Code's ``hooks`` object, from the ``hooks``
argument and from settings files (``{"hooks": {...}}``; the ``noeta``
command reads ``~/.noeta/settings.json``)::

    {"PreToolUse": [{"matcher": "Bash|Edit",
                     "hooks": [{"type": "command", "command": "check.sh", "timeout": 30,
                                "if": "Bash(git *)"}]}]}

Hook types (``config.py`` has the fields and defaults):

* ``command`` — ``sh -c`` on this machine (``args``: the program run
  directly, no shell), in the task's working directory when it exists here,
  the event as JSON on stdin, noeta's environment (minus its own
  credentials) plus ``NOETA_PROJECT_DIR`` (and ``CLAUDE_PROJECT_DIR`` for Claude Code's
  scripts). ``async`` ones run in the background, unread.
* ``http`` — the event POSTed as JSON; ``$VAR`` in ``headers`` is filled
  only for names in ``allowedEnvVars``; the response body is read like
  stdout.
* ``prompt`` — the task's model (or ``model``) judges the hook's condition
  (``$ARGUMENTS`` is the event JSON); not ``ok`` blocks.
* ``agent`` — like ``prompt``, but the model may first look with the
  task's read-only tools (up to 50 calls, a minute by default) and answers
  through a ``StructuredOutput`` tool; never on PermissionRequest.
* ``mcp_tool`` — calls ``tool`` of the MCP ``server`` the task has, with
  ``input`` (``${tool_input.file_path}``-style paths filled from the event);
  its text answer is read like stdout, an error result is a failure.

Exit 0 is success (stdout plain text, or a JSON object: ``continue`` /
``stopReason``, ``decision`` / ``reason``, ``systemMessage``,
``hookSpecificOutput``); exit 2 blocks with stderr as the reason; anything
else is reported and ignored (``onFailure: "block"`` makes it block).
Every event's input carries ``session_id`` (the root task's id),
``transcript_path`` (empty), ``cwd``, ``permission_mode``,
``hook_event_name``, noeta's ``task_id``, and ``agent_id`` /
``agent_type`` inside a sub-agent.

Events:

* ``SessionStart`` (``startup``, ``resume``, ``compact``) /
  ``SubagentStart`` — what they add reaches the model.
* ``UserPromptSubmit`` — before the model first sees new messages from the
  person (mid-turn ones too): adds context, or a block fails the turn and
  erases those messages and their command or skill expansions.
* ``PreToolUse`` — ``permissionDecision`` ``deny`` / ``ask`` / ``allow``
  (an allow skips asking, but a deny or ask rule still wins);
  ``updatedInput`` rewrites the call, which is then checked again.
* ``PermissionRequest`` — when a call would wait for the person: the hook
  allows (optionally rewriting) or denies in their place.
* ``PermissionDenied`` — after the auto-mode classifier denied a call;
  ``retry`` tells the model it may try again.
* ``PostToolUse`` / ``PostToolUseFailure`` — a block or
  ``additionalContext`` follows the result; ``updatedToolOutput`` replaces it.
* ``Stop`` / ``SubagentStop`` — when the model ends its turn (or a
  sub-agent hands back): a block makes it go on, with the reason as its next
  message (at most :data:`MAX_STOP_CONTINUATIONS` times in a row).
* ``PreCompact`` / ``PostCompact`` — stdout adds summary instructions / is
  shown to the person; a PreCompact block skips the compaction.
* ``PostToolBatch`` — once every call of a reply has its result, before
  the next model call: added context (or a block's reason) reaches the model.
* ``PostModelSwitch`` — after a front end switched the model; added context
  reaches the model.
* ``Notification`` (waiting on an approval or a question), ``SessionEnd``,
  ``StopFailure`` (a turn ended on a failed model call; ``error`` is Claude
  Code's kind), ``CwdChanged`` (a command left the shell elsewhere) and
  ``InstructionsLoaded`` (once per instruction file the instructions plugin
  shows; matched on ``load_reason``) — side effects only.
"""

from __future__ import annotations

from noeta.plugins.hooks.config import EVENTS, HookCommand, Matcher, parse_hooks
from noeta.plugins.hooks.extension import MAX_STOP_CONTINUATIONS, Hooks
from noeta.plugins.hooks.runners import (
    HookResult,
    run_agent,
    run_command,
    run_http,
    run_mcp_tool,
    run_prompt,
)

__all__ = [
    "EVENTS",
    "MAX_STOP_CONTINUATIONS",
    "HookCommand",
    "HookResult",
    "Hooks",
    "Matcher",
    "parse_hooks",
    "run_agent",
    "run_command",
    "run_http",
    "run_mcp_tool",
    "run_prompt",
]
