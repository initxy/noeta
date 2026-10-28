# /loop: a recurring or self-paced prompt

Split the input at the end into `[interval] <prompt…>` and schedule it.

## Reading the input (first rule that applies)

1. **Leading interval**: when the first whitespace-separated word matches `^\d+[smhd]$` (such as `5m` or `2h`), it is the interval and the rest is the prompt.
2. **Trailing "every"**: otherwise, when the input ends with `every <N><unit>` or `every <N> <unit word>` (`every 20m`, `every 5 minutes`, `every 2 hours`), that is the interval; remove it from the prompt. Only a time counts: `check every PR` has no interval.
3. **No interval**: otherwise the whole input is the prompt and you pace it yourself (dynamic mode, below).

An empty prompt: show the usage `/loop [interval] <prompt>` and stop. A completely empty input is an autonomous loop (below).

Examples: `5m /babysit-prs` → interval `5m`, prompt `/babysit-prs`; `check the deploy every 20m` → `20m`, `check the deploy`; `run tests every 5 minutes` → `5m`, `run tests`; `check the deploy` → dynamic mode; `check every PR` → dynamic mode; `5m` → usage.

## Fixed interval (rules 1 and 2)

Turn the interval into a cron expression:

| interval | cron | note |
|---|---|---|
| `Nm`, N ≤ 59 | `*/N * * * *` | every N minutes |
| `Nm`, N ≥ 60 | `0 */H * * *` | in hours (H = N/60, must divide 24) |
| `Nh`, N ≤ 23 | `0 */N * * *` | every N hours |
| `Nd` | `0 0 */N * *` | every N days at local midnight |
| `Ns` | as `ceil(N/60)m` | cron counts whole minutes |

When the interval does not divide its unit evenly (`7m` gives an uneven gap from :56 to :00; `90m` is 1.5 h, which cron cannot say), take the nearest even interval and tell the user what you rounded to before scheduling.

1. Call CronCreate with the expression as `cron`, the prompt verbatim as `prompt`, and `recurring: true`.
2. Confirm briefly: what is scheduled, the expression, the cadence in words, that recurring jobs expire after 7 days, and that CronDelete cancels it sooner (give the job id).
3. **Then run the prompt right away**; do not wait for the first firing. A slash command runs through the Skill tool; anything else you act on directly.

## Dynamic mode (rule 3)

The user wants you to set the pace. Decide what makes the next iteration worth running: time passing, or an event you can observe.

1. **Run the prompt now.** A slash command runs through the Skill tool; anything else you act on directly.
2. **When the next run depends on an event** (CI finishing, a log line, a file changing, a PR comment) and no Monitor watches it yet, start one. Its events arrive as task notifications and wake the loop at once, before any ScheduleWakeup deadline. Start it once; on later iterations check that it is still running before starting another.
3. **Decide whether the loop goes on.** If another iteration is needed, call ScheduleWakeup with `delaySeconds` (the fallback heartbeat when a Monitor is the wake signal, 1200–1800 s; otherwise the pace, chosen from what you saw; the tool's description explains the cache-aware choice), `reason` (one sentence on why that delay), `prompt` (the whole original /loop input, starting with `/loop `, so the next firing comes back here: for `/loop check the deploy`, pass `/loop check the deploy`) and `noop` (true when this iteration changed nothing). Otherwise stop (step 6); continuing is a choice made each turn, not the default.
4. **After scheduling, confirm briefly**: that you pace this loop yourself, whether a Monitor is the wake signal, that you ran the task now, and the delay you chose. Write it as ordinary visible text, the last thing in the turn, then end the turn; the user cannot see your thinking.
5. **Woken by a task notification** instead of this prompt: handle the event as part of the loop's task, then decide the same way. To continue, call ScheduleWakeup again with the same `prompt` and the same 1200–1800 s fallback, then write the same short update. If the event means the work is done, stop.
6. **To stop** (the task is done, more iterations cannot help, or the user asked): call ScheduleWakeup with `stop: true` and no other field, and stop any Monitor you started with TaskStop. Then tell the user the loop's outcome. Stopping is how a loop normally ends; the user can start it again with /loop.

## Autonomous loop (empty input)

There is no prompt: keep the work of this conversation moving on your own. Each iteration, look at where the work stands, do the next useful step, and decide whether to continue as in dynamic mode, passing the literal `<<autonomous-loop-dynamic>>` as `prompt`; it expands to these instructions when the wake-up fires.

## Input

{args}
