Leaves the worktree EnterWorktree moved this session into and returns the shell to where it was before.

It acts only on that worktree: never on one made by hand with `git worktree add`, one from an earlier session, or the current directory when EnterWorktree was not used. Without an active worktree session it changes nothing and says so.

Use it only when the user asks to leave or exit the worktree or to go back; never on your own.

- `action: "keep"` leaves the worktree and its branch on disk, for work to come back to or changes to keep.
- `action: "remove"` deletes the worktree and its branch, for work that is done or dropped. When that would lose work (files not yet committed, or commits the starting branch lacks), the tool refuses and says what it found; check with the user, then call again with `discard_changes: true`.

Afterwards EnterWorktree can start a new worktree.
