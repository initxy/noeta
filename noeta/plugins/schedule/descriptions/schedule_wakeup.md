Set when the next iteration of a self-paced `/loop` runs. Use it only in `/loop` dynamic mode: the user started `/loop` with no interval, so you decide the pace of the iterations.

Background work the harness tracks (a sub-agent, a background command, a monitor) wakes you by itself when it finishes, so never schedule short wake-ups to poll it; schedule one long fallback instead (1200 s or more), in case that work stalls or never reports. Work outside the harness (a CI run, a deploy, a remote queue) is the exception: wait about as long as that state takes to change.

Each turn, pass the same `/loop` input back as `prompt`; that is what makes the next wake-up run the task again. A loop the user started with no prompt at all (an autonomous loop) passes the literal sentinel `<<autonomous-loop-dynamic>>` instead, which is expanded into the loop's instructions when the wake-up fires. `<<autonomous-loop>>` is the sentinel of loops driven by CronCreate; never pass it here. To stop the loop, call this tool with only `stop: true`: it ends at once and nothing fires again.

## Choosing delaySeconds

This session's prompt cache lives 5 minutes. A wake-up after more than 300 seconds reads the whole conversation again without the cache, which is slower and costs more. So:
- 60–270 s keeps the cache warm: right while actively polling outside state the harness cannot report, such as a CI run, a deploy or a remote queue.
- 300–3600 s accepts one cache miss: right when nothing can change sooner, when waiting on something slow, when idle, or as the long fallback while something else is the real wake signal.

Never pick exactly 300 s: it pays the cache miss without buying a long wait. Go down to 270 s to stay cached, or up to 1200 s or more so one miss buys a long wait. Think in cache windows, not round minutes.

Idle iterations with nothing specific to watch default to 1200–1800 s (20–30 minutes): the loop still checks back, the cache is not rebuilt a dozen times an hour for nothing, and the user can interrupt whenever they want you sooner.

Size the wait to what you are waiting for: a CI run that takes about 8 minutes is better checked twice at about 270 s than eight times at 60 s.

The runtime clamps the delay to [60, 3600]; you do not need to.

## reason

One short, specific sentence on the delay you chose ("watching the CI run", not "waiting"). The user reads it to follow your pace, so make it concrete.

## noop

`noop: true` when you checked and nothing changed ("still waiting", "no change"); `noop: false` when something worth keeping happened: you edited a file, posted a message, moved the work forward or found something. Consecutive `noop: true` iterations are folded together in the user's view and counted as a streak. Leave `noop` out when stopping.
