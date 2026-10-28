Leaves the worktree EnterWorktree moved this session into and returns the shell to where it was before.

It acts only on that worktree: never on one made by hand with `git worktree add`, one from an earlier session, or the current directory when EnterWorktree was not used. Without an active worktree session it changes nothing and says so.

Use it only when the user asks to leave or exit the worktree or to go back; never on your own.

- `action: "keep"` leaves the worktree and its branch on disk, for work to come back to or changes to keep.
- `action: "remove"` deletes the worktree and its branch, for work that is done or dropped. If the worktree has uncommitted files or commits not on the original branch, the tool refuses and lists them; confirm with the user before calling again with `discard_changes: true`.

Afterwards EnterWorktree can start a new worktree.
