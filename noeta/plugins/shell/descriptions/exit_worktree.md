Leaves the worktree EnterWorktree moved this session into and returns the shell to where it was before.

The tool works from what EnterWorktree recorded in this session and nothing else, so a worktree added with `git worktree add` or left from an earlier session is out of its reach. When there is no such record, it reports that and leaves everything as it is.

Use it only when the user asks to leave or exit the worktree or to go back; never on your own.

- `action: "keep"` leaves the worktree and its branch on disk, for work to come back to or changes to keep.
- `action: "remove"` deletes the worktree and its branch, for work that is done or dropped. When that would lose work (files not yet committed, or commits the starting branch lacks), the tool refuses and says what it found; check with the user, then call again with `discard_changes: true`.

Afterwards EnterWorktree can start a new worktree.
