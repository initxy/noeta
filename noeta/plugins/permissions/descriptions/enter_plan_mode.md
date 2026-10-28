Switches to plan mode, where you explore the code and design an approach for the user to approve before any code is written. Reach for it on your own before a non-trivial implementation: agreeing on the approach first saves work that would otherwise be redone.

Use it when any of these holds:
- A new feature: even a small one leaves open where it goes and how it behaves.
- Several reasonable ways to do it: a cache could live in memory, on disk or in a service.
- A change to existing behavior or structure, where what exactly should change is not settled.
- A choice between patterns or technologies (polling or push, which state library).
- Work that will likely touch more than two or three files.
- Requirements you must investigate before you know the scope (a slow page, a bug with an unknown cause).
- The user's preference matters: if you would ask them with AskUserQuestion how to go about it, plan instead, so they choose with the context in front of them.

Skip it for:
- A fix of a line or a few lines: a typo, an obvious bug, a small tweak.
- One function whose requirements are clear.
- A task the user already spelled out in detail.
- Research or exploration with nothing to implement.

Entering plan mode needs the user's consent. When unsure, plan: agreeing first costs less than redoing the work.
