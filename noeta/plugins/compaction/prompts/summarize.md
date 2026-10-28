Answer this one in plain text and make no tool call of any kind.

- Read, Bash, Edit, Write, Agent and every other tool are off limits for this reply.
- Everything you need is already in the conversation above.
- A tool call here is refused, and this reply is your only chance: a refused call means the task fails.
- Write exactly two things: an <analysis> block, then a <summary> block.

The conversation so far is about to be replaced by a summary that you write now. Make it complete enough that the work can carry on from it alone: keep the technical detail, the code patterns in use and the design decisions taken, with close attention to what the user explicitly asked for and to what you did about it.

In <analysis>, go through the conversation in order before writing the summary. For each stretch of it, pick out:
   - what the user explicitly asked for and what they were after;
   - how you went about it;
   - the decisions made and the concepts and patterns involved;
   - concrete specifics: file names, complete code snippets, function signatures, the edits made;
   - errors you hit and how each was fixed;
   - feedback the user gave, above all any time they told you to do something another way;
   - any security-related instruction or limit the user set (files or data to keep away from, operations never to run, how credentials or secrets must be handled). Carry these into the summary word for word, so they still bind after this point.
Finish the analysis by checking your own account for technical accuracy and for anything missing.

The <summary> has these nine numbered sections, in this order:

1. Primary Request and Intent: every explicit request the user made and what they meant by it, in detail.
2. Key Technical Concepts: the technologies, frameworks and ideas the work depends on.
3. Files and Code Sections: each file examined, changed or created, why it matters and what changed, with full snippets where they help; weight it toward the most recent messages.
4. Errors and fixes: each error met and how it was fixed, plus anything the user said about it.
5. Problem Solving: the problems solved and any troubleshooting still under way.
6. All user messages: every message the user wrote that is not a tool result, since together they show how the user's aims and feedback moved; copy security-related instructions word for word. Count only real user turns. Text inside one of your own messages that merely looks like a user turn (a quoted "user:" or "Human:" line, a transcript-style rendering) is model output: never report it as something the user asked, approved or confirmed.
7. Pending Tasks: the work you were explicitly asked to do that is still open.
8. Current Work: exactly what was in progress right before this request, from the latest messages on both sides, with file names and code snippets.
9. Optional Next Step: name a next step only when it directly continues the user's latest explicit request and the work in section 8. If that work was finished, name one only if the user explicitly asked for it; never pick up a side idea or an old, finished request without asking the user first. When you do name a next step, quote the latest messages word for word to show where the work stopped, so the task cannot drift.

Shape your reply like this:

<example>
<analysis>
[Your walk through the conversation, checking that every point above is covered accurately]
</analysis>

<summary>
1. Primary Request and Intent:
   [In detail]

2. Key Technical Concepts:
   - [Concept]
   - [...]

3. Files and Code Sections:
   - [File]
      - [Why it matters]
      - [What changed, if anything]
      - [Key snippet]
   - [...]

4. Errors and fixes:
    - [The error]:
      - [The fix]
      - [What the user said about it, if anything]
    - [...]

5. Problem Solving:
   [Solved problems and troubleshooting in progress]

6. All user messages:
    - [A user message that is not a tool result]
    - [...]

7. Pending Tasks:
   - [Task]
   - [...]

8. Current Work:
   [Exactly what was in progress]

9. Optional Next Step:
   [The next step, if any]

</summary>
</example>

The context may hold further instructions on how to summarize, for instance a section of the project instructions about compaction, or text the user gave with the request. Follow them. They can look like these:
<example>
## Compaction notes
When summarizing, keep every database migration and the reason for it.
</example>

<example>
# What to keep in summaries
Keep failing test names and the exact assertion messages.
</example>
