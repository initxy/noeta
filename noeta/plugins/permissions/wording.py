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

# ---------------------------------------------------------------- person-facing

ASK_EDIT = "{name} wants to change {path}"
ASK_CREATE = "{name} wants to create {path}"
ASK_COMMAND = "This command needs your approval"
ASK_READ_OUTSIDE = "{name} wants to read {path}, outside the working directories"
ASK_PROTECTED = "{name} would change {path}, which configures tools or the agent itself"
ASK_PLAN = "{name} would change things while in plan mode"
ASK_RULE = "The rule {rule} asks before {name} runs"
ASK_TOOL = "{name} needs your approval"
ASK_FETCH = "{name} wants to fetch {host}"
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

#: Auto mode's note on using the shell for small jobs (``rem.auto-mode.*``).
AUTO_MODE_GUIDANCE = (
    "Auto mode is on, so the shell may also do small jobs: reading (cat, head, sed -n), "
    "searching (grep, find) and small mechanical edits (sed, heredocs, short scripts) "
    "instead of Read, Edit or Write. Use whichever fits. Prefer Edit or Write when a shell "
    "edit would be brittle: exact or multi-line replacements, or flags that differ between "
    "GNU and BSD tools.")
