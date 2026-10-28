Creates a git worktree and moves this session's shell into it, so the work happens on its own branch in an isolated copy of the repository.

Use it only when worktrees were asked for: the user says "worktree" (start one, work in one, use one), or the project's instructions or memory say to work in a worktree for this task. Otherwise do not use it: to create or switch a branch, use git; to fix a bug or build a feature, work as usual.

How it behaves:
- It needs a git repository. A new worktree (`name`, or a random name) goes under `.noeta/worktrees/` on a new branch starting from the remote's default branch when there is one, else from the current commit. Creating one while already in a worktree is refused.
- Bash commands start in the worktree from then on; give file tools absolute paths inside it.
- Leave it with ExitWorktree, keeping or removing it.
- With `path` instead of `name`, it moves into a worktree that already exists inside the working directory (as `git worktree list` shows); from inside a worktree only another one under `.noeta/worktrees/` of the same repository. ExitWorktree never removes a worktree entered this way.
