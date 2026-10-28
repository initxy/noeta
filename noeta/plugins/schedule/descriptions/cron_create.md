Queue a prompt to run later: on a recurring cron schedule, or once at a set time. The schedule is a standard 5-field cron expression in the user's local time zone (minute, hour, day of month, month, day of week); "0 9 * * *" is 9 in the morning local time, with no time-zone conversion.

## One-shot (recurring: false)

For "remind me at X" or "at <time>, do Y": the job runs once and then deletes itself. Pin minute, hour, day of month and month:
  "remind me at 2:30pm today to check the deploy" → cron "30 14 <today's day> <this month> *", recurring false
  "tomorrow morning, run the smoke test" → cron "57 8 <tomorrow's day> <its month> *", recurring false

## Recurring (recurring: true, the default)

For "every N minutes", "every hour", "weekdays at 9":
  "*/5 * * * *" (every 5 minutes), "0 * * * *" (hourly), "0 9 * * 1-5" (weekdays at 9 local)

## Stay off :00 and :30 when the request allows

Everyone who asks for "9am" gets `0 9` and everyone who asks for "hourly" gets `0 *`, so requests from everywhere hit the API in the same instant. When the time is approximate, pick a minute other than 0 or 30:
  "every morning around 9" → "57 8 * * *" or "3 9 * * *", not "0 9 * * *"
  "hourly" → "7 * * * *", not "0 * * * *"
  "in an hour or so, remind me to…" → whatever minute you land on, unrounded
Use minute 0 or 30 only when the user names that exact time and means it ("at 9:00 sharp", "at half past", lining up with a meeting). When unsure, move a few minutes early or late: the user will not notice, and the load on the service will.

## Durability

Jobs are stored in this task's log, so they survive a restart and fire on whichever worker runs the task; `durable` is accepted for compatibility and changes nothing.

## Not for live watching

This tool reruns a prompt at fixed wall-clock times. To be told the moment a log file, a process or a command's output changes, use the Monitor tool instead: it streams events as they happen, cron polls on a schedule.

## How it runs

A job fires only while the session is idle, never in the middle of a turn. The scheduler shifts every job by a fixed offset of its own on top of the minute you pick: a recurring job runs up to half its period late (at most 30 minutes), and a one-shot job that lands on :00 or :30 runs up to 90 seconds early. The result of this tool and of CronList give each job's actual next run; when you tell the user when a job runs, give that time, not the minute in the expression. Picking an off-minute still matters more.

Recurring jobs expire after 7 days: they fire one last time and are deleted, which bounds how long a task keeps itself busy. Tell the user about the 7-day limit when you schedule a recurring job.

Returns a job id to pass to CronDelete.
