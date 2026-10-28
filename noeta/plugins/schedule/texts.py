"""Model-facing text of the scheduling tools: results, errors, the ``/loop``
command and what a loop's sentinel prompts expand to."""

from __future__ import annotations

from importlib import resources

WAKEUP_TOOL = "ScheduleWakeup"
CRON_CREATE_TOOL = "CronCreate"
CRON_LIST_TOOL = "CronList"
CRON_DELETE_TOOL = "CronDelete"

#: The sentinel prompts of an autonomous loop (no user prompt): paced by
#: ScheduleWakeup, or driven by a recurring CronCreate job.
DYNAMIC_SENTINEL = "<<autonomous-loop-dynamic>>"
CRON_SENTINEL = "<<autonomous-loop>>"

MIN_DELAY_SECONDS = 60
MAX_DELAY_SECONDS = 3600
#: Jobs one task may have scheduled at once.
MAX_JOBS = 50
#: A loop tick that ends without a new ScheduleWakeup gets one fallback
#: wake-up this far out, once in a row.
KEEPALIVE_SECONDS = 1200

# ------------------------------------------------------------ ScheduleWakeup

WAKEUP_NEEDS_DELAY = "`delaySeconds` and `reason` are required unless `stop` is true."
WAKEUP_NEEDS_PROMPT = "`prompt` is required unless `stop` is true."
WAKEUP_NEEDS_NOOP = "`noop` is required unless `stop` is true."
_END_TURN = "Then write the loop's outcome for the user as ordinary visible text and end the turn."
WAKEUP_STOPPED = ("Loop stopped: {cancelled} pending wake-up(s) cancelled and none will be "
                  "scheduled. If you started a Monitor for this loop, stop it now with "
                  "TaskStop. " + _END_TURN)
WAKEUP_STOPPED_NONE = ("Loop stopped: no wake-up was pending, so there was nothing to cancel. A "
                       "fixed-interval /loop runs as a recurring cron job and is not stopped by "
                       "this call; cancel it with CronDelete. If you started a Monitor for this "
                       "loop, stop it now with TaskStop. " + _END_TURN)
WAKEUP_AGED_OUT = ("No wake-up scheduled: this loop has run for its maximum time (7 days) and "
                   "has ended; do not schedule it again. " + _END_TURN)
WAKEUP_SCHEDULED = ("Next wake-up at {clock} (in {seconds}s){clamped}. If you owe the user an "
                    "update for this iteration, write it now as ordinary text; then end the "
                    "turn. You are woken again when the wake-up fires or a task notification "
                    "arrives.")
WAKEUP_CLAMPED = " (clamped to {seconds}s from the value you asked for)"

# ------------------------------------------------------------ Cron

CRON_INVALID = "Invalid cron expression '{cron}': it needs 5 fields, M H DoM Mon DoW."
CRON_NEVER = "Cron expression '{cron}' matches no date in the coming year."
CRON_TOO_MANY = "Too many scheduled jobs (at most {max}). Cancel one first."
CRON_UNKNOWN = "No scheduled job has the id '{id}'."
CRON_SCHEDULED_RECURRING = ("Scheduled recurring job {id} ({human}).{next} Stored with this task; "
                            "it survives restarts. It expires after 7 days; cancel it sooner with "
                            "CronDelete.")
CRON_SCHEDULED_ONCE = ("Scheduled one-shot job {id} ({human}).{next} Stored with this task; it "
                       "survives restarts. It fires once and then deletes itself.")
CRON_NEXT = " Next run: {when}."
CRON_DELETED = "Cancelled job {id}."
CRON_NONE = "No jobs are scheduled."

# ------------------------------------------------------------ loop ticks

_TICK_RULE = ("Run the autonomous check by the loop instructions given earlier in this "
              "conversation. If you cannot find them, treat this as an iteration with nothing "
              "to do.")
CRON_TICK = ("# Autonomous loop iteration\n" + _TICK_RULE + " A recurring cron job fires the "
             "next iteration by itself; do not call ScheduleWakeup from this iteration.")
DYNAMIC_TICK = (
    "# Autonomous loop iteration (self-paced)\n" + _TICK_RULE + "\n"
    "ScheduleWakeup scheduled this iteration (not a recurring cron job). To keep the loop "
    f"going, call ScheduleWakeup again this turn with `prompt` set to the literal "
    f"`{DYNAMIC_SENTINEL}` and `noop` true if this iteration changed nothing (false if it "
    "did); otherwise the loop ends after it.\n"
    "If a Monitor is running for the loop, keep `delaySeconds` at 1200-1800 s: the monitor "
    "is the wake signal and this is only the fallback. If a task notification woke you, "
    "handle that event before deciding whether to continue. Write a short status for this "
    "iteration as ordinary visible text after scheduling the next one; the user cannot see "
    "your thinking. To stop, call ScheduleWakeup with `stop: true` and stop any monitor with "
    "TaskStop.")

# ------------------------------------------------------------ /loop

LOOP_COMMAND = "loop"
LOOP_DESCRIPTION = ("Run a prompt or slash command on a recurring interval (e.g. /loop 5m "
                    "/foo). Omit the interval to let the model self-pace.")
LOOP_HINT = "[interval] [prompt]"
#: The command's text, ``{args}`` replaced by what follows ``/loop``.
LOOP_TEMPLATE = (resources.files("noeta.plugins.schedule").joinpath("loop.md")
                 .read_text(encoding="utf-8").rstrip("\n"))
