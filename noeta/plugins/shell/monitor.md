Runs a script in the background and turns every line it prints on stdout into a notification for you, while you go on working. A notification is an event from the script, never the user's reply, even when it arrives while you wait for the user.

Choose by how many notifications the task needs:
- A single one ("tell me when the build is done", "when the server is up"): not this tool. Use Bash with `run_in_background` and a command that exits once the condition holds, such as `until curl -sf localhost:8080/health; do sleep 1; done`; its exit is your one notice.
- One per occurrence, open-ended ("every time a test fails"): Monitor with a command that never ends on its own (`tail -f`, `inotifywait -m`, a `while true` loop); it runs until it expires and you re-arm it.
- One per occurrence until a known end ("report each deploy step, stop when the deploy finishes"): Monitor with a command that prints lines and then exits.

Examples of event sources:

  # every ERROR line of a log
  tail -f app.log | grep --line-buffered ERROR

  # every change in a directory
  inotifywait -m --format '%e %f' ./src

  # new review comments, polled every 30 seconds
  while true; do gh api repos/o/r/pulls/7/comments --jq '.[].body' > now.txt; comm -13 seen.txt now.txt; mv now.txt seen.txt; sleep 30; done

  # each finished CI check, ending when none is pending
  while true; do out=$(gh pr checks 7); grep -v pending <<<"$out" | sort > done.txt; comm -13 old.txt done.txt; mv done.txt old.txt; grep -q pending <<<"$out" || break; sleep 30; done

Never use an open-ended command for a single notification: `tail -f`, `inotifywait -m` and `while true` do not stop by themselves, so the monitor stays armed until it expires. `tail -f log | grep -m 1 x` does not help either: once the log goes quiet `tail` is never told the pipe closed.

Writing the script:
- Each stage of a pipe must emit line by line or events sit in its buffer: `grep --line-buffered`, `fflush()` in `awk`; `head -N` holds everything until it has N lines.
- In a polling loop, let one failed request pass (`curl ... || true`) instead of ending the monitor.
- Poll remote APIs every 30 seconds or slower, local state every 0.5 to 1 second.
- Make `description` specific ("failures in deploy.log", not "logs"): it is shown with every event.
- Only stdout produces events. stderr does not; you see its last lines in the notice sent when the script ends. For a command you run yourself, add `2>&1` so its errors reach your filter.

Silence is not success: a filter that only matches the good outcome says nothing when the process crashes, hangs or exits early, which looks the same as "still running". Make the filter match every final state you would act on (for example `grep -E --line-buffered "passed|failed|Traceback|Killed|OOM"`), and in polling loops emit every terminal status, not only success. When unsure, match more rather than less.

Every stdout line becomes a message in the conversation, so pass only the lines you would act on and never a raw log. A monitor that emits too fast is muted and then stopped; start it again with a narrower filter. Lines printed within 200 ms of each other arrive as one notification.

The script runs in the same shell environment as Bash. When it exits the watch ends and its exit code is reported. Each monitor expires after `timeout_ms` (default 5 minutes, at most 30 minutes): it is stopped and you get one notice with the number of events. Re-arm it if you still need it; for a long watch, use the maximum and re-arm at each expiry. Stop it early with TaskStop.

**WebSocket source**: instead of `command`, pass `ws` to connect to a WebSocket and turn every text message the server pushes into an event; nothing runs in a shell and nothing polls.

  Monitor({ws: {url: 'wss://events.example.com/stream', protocols: ['v1']}, description: 'deploy events'})

A text message is one notification, however many lines it has. A binary message shows as `[binary frame, N bytes]`, never its content. The watch ends when the socket closes, and the notice gives the close code; a connection error is reported before that. Rate limits are those of a command, so subscribe to a filtered feed where the server offers one. Prefer this to a WebSocket client run in `command`: no extra process and no line buffering to get wrong. Use a command when the messages need shell tools to filter or reshape them first.
