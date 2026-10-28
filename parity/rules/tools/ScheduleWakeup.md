# ScheduleWakeup

Claude Code 2.1.312 · baseline `m1` · rules in our words (see `../README.md`).

| source | sha256 |
|---|---|
| `m1/0001 tools[ScheduleWakeup].description` | `98e2e57f316f9b39f742470acbadbc908786aaf44b10a757b6c15cd68e590e85` |
| `m1/0001 tools[ScheduleWakeup].input_schema` | `81fd95134e75587b1ce6124ff43f3afc320f5ee0fbe4e73af86c75c7e7e6887e` |
| `s1/0002 tools[ScheduleWakeup].description` (variant, not baseline) | `cd30d1db77050fe62b52b7c0f83474c81d950336cb47b29325c18be9f7d7144e` |

## Purpose

Schedules the next iteration of a self-paced `/loop` (a `/loop` without an interval). Always
loaded; position 8 of 13 in the baseline list. Never called in the recordings. noeta:
`noeta/plugins/schedule` (with the deferred `CronCreate` / `CronList` / `CronDelete` and the `/loop`
command), on the kernel's timers.

## Parameters

`additionalProperties: false`; nothing required by the schema — `delaySeconds`, `reason`,
`prompt` and `noop` are documented as required unless `stop` is true.

| id | name | type | required | enum / default / limits | noeta today |
|---|---|---|---|---|---|
| `tool.ScheduleWakeup.p.delaySeconds` | `delaySeconds` | number | unless `stop` | seconds from now; the runtime clamps to [60, 3600] | covered |
| `tool.ScheduleWakeup.p.reason` | `reason` | string | unless `stop` | one short, specific sentence on the chosen delay; sent to telemetry and shown to the user | covered |
| `tool.ScheduleWakeup.p.prompt` | `prompt` | string | unless `stop` | the `/loop` input to fire, passed back verbatim every turn; `<<autonomous-loop-dynamic>>` for an autonomous loop | covered |
| `tool.ScheduleWakeup.p.stop` | `stop` | boolean | no | true ends the loop at once; all other fields ignored | covered |
| `tool.ScheduleWakeup.p.noop` | `noop` | boolean | unless `stop` | true = checked, nothing to report; false = something worth keeping happened | covered |

## Rules

| id | rule | noeta today |
|---|---|---|
| `tool.ScheduleWakeup.1` | Only for `/loop` dynamic mode: the user started `/loop` without an interval and the model paces the iterations. | covered — `/loop` is a command (`noeta/plugins/schedule/loop.md`) |
| `tool.ScheduleWakeup.2` | Never schedule a short wakeup to poll background work the harness tracks — its completion re-invokes the model; instead schedule a long fallback (1200 s or more) in case it hangs. | covered |
| `tool.ScheduleWakeup.3` | Exception: external state the harness cannot track (CI run, deploy, remote queue) gets a delay matched to how fast that state changes. | covered |
| `tool.ScheduleWakeup.4` | Each turn hands the identical `/loop` prompt back, which is what makes the next wake-up run the task again. | covered — a `/loop` prompt is expanded when scheduled and fired as the next message |
| `tool.ScheduleWakeup.5` | An autonomous loop (no user prompt) passes the sentinel `<<autonomous-loop-dynamic>>`, which the runtime expands at fire time; `<<autonomous-loop>>` belongs to `CronCreate` loops and must not be used here. | covered |
| `tool.ScheduleWakeup.6` | To end the loop call with `stop: true` and no other field; no further wakeups fire. | covered |
| `tool.ScheduleWakeup.7` | `noop: true` when nothing changed; `false` when a file was edited, a message posted, state advanced or a finding surfaced; omit `noop` when stopping. | covered |
| `tool.ScheduleWakeup.8` | Consecutive `noop: true` ticks are collapsed in the terminal and counted as a streak. | covered — the description says so; noop ticks reach front ends as `loop_wakeup` notices, no front end folds them yet |
| `tool.ScheduleWakeup.9` | Delay choice follows the prompt-cache TTL, stated in the description: 5 minutes for this (API-key) session, so a sleep past 300 s re-reads the whole context uncached. | covered |
| `tool.ScheduleWakeup.10` | 60–270 s keeps the cache warm — for actively polling external state. | covered |
| `tool.ScheduleWakeup.11` | 300–3600 s accepts one cache miss — for waits on slow changes, idleness, or a fallback heartbeat when something else is the main wake signal. | covered |
| `tool.ScheduleWakeup.12` | Never pick exactly 300 s (cache miss without the long wait): go down to 270 s or up to 1200 s+. Think in cache windows, not round minutes. | covered |
| `tool.ScheduleWakeup.13` | Idle ticks with no specific signal default to 1200–1800 s. | covered |
| `tool.ScheduleWakeup.14` | Size the delay to what is awaited: for a ~8 minute CI run, about 270 s twice rather than 60 s eight times. | covered |
| `tool.ScheduleWakeup.15` | No need to clamp; the runtime clamps to [60, 3600]. | covered |
| `tool.ScheduleWakeup.16` | `reason`: one specific sentence ("watching CI run", not "waiting"), read by the user to follow the cadence. | covered |

## Result shape

| id | shape (observed) |
|---|---|
| `tool.ScheduleWakeup.r.1` | Not observed (never called). |

## Variants

- s1 (OAuth, 1-hour cache TTL): the description is rebuilt around a 1-hour TTL — no cache cliff
  inside [60, 3600], never schedule wakeups just to keep the cache warm, overage may drop to the
  5-minute TTL but the guidance stays; the "don't pick 300 s" and 60–270 s cache-window rules
  (`.9`–`.12`, `.14`'s 270 s figure) are replaced by: poll external state at the rate it
  changes (one ~480 s check for an 8 minute CI run), fallback heartbeat 1200 s+, idle
  1200–1800 s. The schema is identical. The description is generated from the session's
  cache TTL, so noeta's text must follow the TTL noeta actually uses.
