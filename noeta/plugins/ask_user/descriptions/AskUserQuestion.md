Asks the user one to four multiple-choice questions and waits for the answers. Use it only when going on depends on a choice that is the user's to make and that the request, the code or a sensible default cannot settle.

- The user can always answer "Other" and type their own text instead of a listed option.
- `multiSelect: true` lets the user pick several options for one question.
- If you recommend an option, list it first and end its label with "(Recommended)".
- Keep it for decisions whose answer changes what you do next. When a choice has a conventional default, or the fact can be checked in the code, take the obvious option, say which, and continue.
- The user may see only the question dialog, not your earlier messages or tool results: make each question stand on its own, say briefly what it depends on, and name the things you ask about.
- To enter plan mode, use EnterPlanMode, not this tool. In plan mode, use it to settle requirements or to pick between approaches before the plan is final; never to ask whether the plan is ready or whether to go ahead, and never mention "the plan" in a question: the user cannot see it until ExitPlanMode.

Previews:
- Give an option a `preview` to compare concrete things: ASCII mockups of a UI, variants of code, of a diagram or of a configuration.
- A preview is shown as Markdown in a monospace box and may span several lines; when any option has one, the dialog puts the options on the left and the focused option's preview on the right.
- Skip previews for plain preference questions; they work only on single-select questions.
