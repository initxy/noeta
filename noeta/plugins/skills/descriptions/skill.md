Runs a skill inside the conversation.

A skill is a packaged set of instructions that the user or the project set up for one kind of task, such as release steps, a review checklist or a repository's workflow. The available skills are listed one line each in a message in this conversation.

- When a listed skill covers the task, call this tool before anything else: its instructions then take the place of your usual approach for this turn.
- Some skills run in a sub-agent and return the finished result instead of loading instructions. One that runs in the background answers with only an agent name; its outcome arrives later in a task notification, so do not wait for it or call the skill again meanwhile.
- When the user names a skill or types `/<name>` (a slash command), they are asking you to run it.
- `skill` is the exact listed name, without a leading slash. A plugin's skill is named `plugin:skill`.
- A skill scoped to a directory has a path prefix (`apps/web:deploy`). When a scoped and an unscoped skill share a name, take the one whose directory contains the files you are working on, the deepest first, and otherwise the unscoped one.
- Only listed names, or names the user typed, are valid.
- Built-in commands such as `/help` or `/clear` are not skills.
- If a `<command-name>` block is already in the current turn, that skill is already loaded: follow it instead of calling this tool again.
