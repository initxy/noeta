Use this tool in plan mode when your plan is written to the plan file and you are ready for the user to approve it.

How it works:
- The plan file's path is given in the plan mode instructions; write the plan there before calling this tool.
- The tool takes no plan text: it reads the plan from that file.
- Calling it only says you are done planning; the user reads the file and decides.

When to use it:
Only for tasks whose plan is a set of implementation steps for writing code. Do not use it for research: searching, reading files or working out how existing code behaves.

Before calling it:
- Make sure the plan is complete and leaves nothing open.
- Settle open questions about requirements or approach with AskUserQuestion first, in an earlier step.
- Once the plan is final, call this tool to ask for approval. Never ask "Is this plan okay?" or "Should I go ahead?" with AskUserQuestion or in text: this tool is that question, and the user sees the plan when you call it.

Examples:
1. "Find out how the cache is invalidated": do not use this tool; it is research, not an implementation.
2. "Add a --verbose flag to the CLI": once the plan for the change is written, use this tool.
3. "Add caching to the API client" when you do not know which backend to use: settle that with AskUserQuestion first, then use this tool.
