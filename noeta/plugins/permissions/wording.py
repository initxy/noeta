"""The permissions plugin's model-facing and person-facing texts, in our own
words (the rules they follow are in ``parity/rules/``)."""

from __future__ import annotations

# ---------------------------------------------------------------- refusals

#: How to react to a refusal: other tools are fine, defeating it is not.
WORK_AROUND = (
    "You may reach the same goal with another tool that would naturally do the job (head "
    "instead of cat, say), but do not get around this refusal in ways that defeat its "
    "purpose, such as using a test run to execute something that is not a test. ")
#: The end of a mode or rule refusal.
STOP_AND_EXPLAIN = (
    "If this capability is essential to the request, stop and tell the user what you were "
    "trying to do and why you need the permission, and let them decide how to go on.")
#: The end of a classifier refusal.
TRY_SAFER_FIRST = (
    "If it is essential to the request, first look for a safer way to do it. Finish as much "
    "of the rest of the task as you can, then stop and tell the user what you were trying "
    "to do and why you need the permission, and let them decide how to go on.")

DONT_ASK_DENIED = ("Permission to use {name} was refused: noeta runs in don't-ask mode, which "
                   "refuses whatever is not allowed in advance. " + WORK_AROUND
                   + STOP_AND_EXPLAIN)
DENIED_BY_RULE = ("Permission to use {name} was refused by the permission rule {rule}. "
                  + WORK_AROUND + STOP_AND_EXPLAIN)
OUTSIDE_WRITE = ("{name} only writes inside the working directories ({dirs}); {path} is "
                 "outside them. If it must change, ask the user to add its directory to the "
                 "additional directories.")
CLASSIFIER_BLOCKED = (
    "The auto-mode classifier refused this action. Reason: {reason}. If other parts of the "
    "task do not depend on it, carry on with those. " + WORK_AROUND + TRY_SAFER_FIRST)
CLASSIFIER_NO_VERDICT = (
    "{reason}. This is not a judgment that the action is unsafe. It is usually temporary: "
    "wait briefly and try the same action again unchanged. If it keeps failing, carry on "
    "with work that does not need it and come back to it later. Reading files, searching "
    "code and other read-only work does not go through the classifier and still works.")
CLASSIFIER_UNAVAILABLE = "The auto-mode classifier could not be reached"
CLASSIFIER_UNPARSED = "The auto-mode classifier gave no readable verdict"
#: Stage 2 failed after stage 1 leaned to blocking: a block (counted).
CLASSIFIER_STAGE2_FAILED = ("The classifier's second stage failed, so the first stage's "
                            "lean to block stands (usually temporary: the same action may "
                            "pass on a later try)")
#: The transcript does not fit the classifier's window, for a tool only the
#: classifier may allow (a sub-agent's hand-back).
CLASSIFIER_TOO_LONG = (
    "{name} was not reviewed: the conversation is too long for the auto-mode classifier, "
    "and nobody is asked about this tool instead. This is not a judgment that the action is "
    "unsafe; the same call fails the same way until the conversation is shorter.")
#: Above a sub-agent's report auto mode flagged (Claude Code delivers it
#: with a warning instead of refusing it).
HANDBACK_FLAGGED = (
    "SECURITY WARNING: auto mode flagged this sub-agent's report. Reason: {reason}. The "
    "report is below; check what the sub-agent did before you act on it.")
HANDBACK_UNREVIEWED = (
    "Note: the auto-mode classifier could not review this sub-agent's report. Check what the "
    "sub-agent did and what it reports before you act on it.")

# ---------------------------------------------------------------- person-facing

ASK_EDIT = "{name} wants to change {path}"
ASK_CREATE = "{name} wants to create {path}"
ASK_COMMAND = "This command needs your approval"
ASK_READ_OUTSIDE = "{name} wants to read {path}, outside the working directories"
ASK_PROTECTED = "{name} would change {path}, which configures tools or the agent itself"
ASK_PLAN = "{name} would change things while in plan mode"
ASK_ENTER_PLAN = "Enter plan mode?"
ASK_RULE = "The rule {rule} asks before {name} runs"
ASK_TOOL = "{name} needs your approval"
ASK_FETCH = "{name} wants to fetch {host}"
ASK_UNSANDBOXED = "{name} wants to run this command outside the sandbox"
DENIAL_LIMIT_CONSECUTIVE = ("{n} actions in a row were blocked by the auto-mode classifier; "
                            "look over the conversation before letting it go on. Latest "
                            "blocked action: {reason}")
DENIAL_LIMIT_TOTAL = ("{n} actions were blocked by the auto-mode classifier in this task; "
                      "look over the conversation before letting it go on. Latest blocked "
                      "action: {reason}")

#: Approval choices front ends show (``Ask.detail["choices"]``).
CHOICE_YES = "Yes"
CHOICE_NO = "No"
CHOICE_ALWAYS_COMMANDS = "Yes, and don't ask again for {rules} in {dir}"
CHOICE_ALWAYS_RULE = "Yes, and don't ask again for {rules}"
CHOICE_ALWAYS_DIR = "Yes, and always allow access to {dir} from this project"
CHOICE_MODE = {
    "auto": "Yes, and switch to auto mode (a classifier screens what would ask)",
    "acceptEdits": "Yes, and switch to accept edits for this session (file edits and file "
                   "commands run without asking)",
}
#: How each tool's card is titled.
TITLES = {"Bash": "Bash command", "Monitor": "Monitor command", "Write": "Create file",
          "Edit": "Edit file", "NotebookEdit": "Edit notebook", "WebFetch": "Fetch",
          "Read": "Read file"}

# ---------------------------------------------------------------- reminders

#: The note on using the shell for small jobs, sent in auto mode and in
#: bypass-permissions mode (``rem.auto-mode.*``); ``{mode}`` names the mode.
SHELL_FIRST_GUIDANCE = (
    "{mode} is on, so the shell may also do small jobs: reading (cat, head, sed -n), "
    "searching (grep, find) and small mechanical edits (sed, heredocs, short scripts) "
    "instead of Read, Edit or Write. Use whichever fits. Prefer Edit or Write when a shell "
    "edit would be brittle: exact or multi-line replacements, or flags that differ between "
    "GNU and BSD tools.")
AUTO_MODE_GUIDANCE = SHELL_FIRST_GUIDANCE.format(mode="Auto mode")
BYPASS_MODE_GUIDANCE = SHELL_FIRST_GUIDANCE.format(mode="Bypass-permissions mode")
#: Sent once auto mode is left after its note went out (planning keeps it,
#: unless plan mode runs without the classifier).
AUTO_MODE_EXITED = (
    "## Auto mode has ended\n\nAuto mode is off now. Go back to the dedicated tools for "
    "reading, searching and editing files.")
