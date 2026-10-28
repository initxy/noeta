Send a message to another agent: a sub-agent you started (by the id from its launch result or ListAgents), your caller ("main", from a background sub-agent), or another task listed by ListAgents.

A sub-agent that is still running gets the message at its next step. One that has finished is resumed from its transcript, which still holds its task, with your message as the next turn; it runs in the background and you are notified when it is done. Your plain text is not visible to other agents; to tell one something, call this tool.

To learn when another open session listed by ListAgents finishes what it is doing, pass `notify_when_idle: true` (main conversation only). It is opt-in and one-shot: a single `[Cross-session idle notice]` reaches you when that session next goes idle (or ends), or, if no signal comes within the subscription's lifetime, a notice that it expired. Leave out `message` for a bare subscription that costs that session nothing; include one to deliver it now and subscribe as well. Never poll a session or keep asking whether it is done.

The receiver takes your words as a message, not as its user's instructions. Permission decisions belong to each session: never ask another agent to do something that was denied or blocked here, or that your permission settings would block; route blocked work back to your user instead.
