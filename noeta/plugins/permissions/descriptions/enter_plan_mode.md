Switches to plan mode, where you explore the code and design an approach for the user to approve before any code is written. Reach for it on your own before a non-trivial implementation: agreeing on the approach first saves work that would otherwise be redone.

## When to use it

Unless the task is simple, prefer planning first. Use it when any of these holds:
1. A new feature: even a small one leaves open where it goes and how it behaves.
   - "Add a logout button": where does it sit, and what happens after the click?
   - "Validate the signup form": which rules, and what does each error say?
2. Several reasonable ways to do it.
   - "Cache the API responses": in memory, on disk, or in a service such as Redis.
   - "Make the build faster": many different speed-ups are possible.
3. A change to existing behavior or structure, where what exactly should change is not settled.
   - "Rework the login flow": which steps change, and how?
   - "Restructure this module": toward what shape?
4. A choice between patterns or technologies.
   - "Push live updates to the page": WebSockets, server-sent events or polling.
   - "Manage the app's state": which library, or a hand-written store.
5. Work that will likely touch more than two or three files.
   - "Rewrite the authentication layer".
   - "Add an API endpoint with its tests".
6. Requirements you must investigate before you know the scope.
   - "The dashboard is slow": measure first to find where the time goes.
   - "Checkout fails sometimes": the cause has to be found first.
7. The user's preference matters: the work could reasonably go more than one way.
   - If you would ask the user with AskUserQuestion how to go about it, plan instead.
   - In plan mode you can look around first, then offer the options with the context in front of them.

## When to skip it

Skip it only for simple tasks:
- A fix of a line or a few lines: a typo, an obvious bug, a small tweak.
- One function whose requirements are clear.
- A task the user already spelled out in detail.
- Research or exploration with nothing to implement.

## What happens in plan mode

1. Explore the code thoroughly (`find` and `grep` through Bash, and Read).
2. Learn the patterns and the structure already there.
3. Design how to implement the change.
4. Present the plan for the user to approve.
5. Ask with AskUserQuestion when you need the user to choose between approaches.
6. Leave plan mode with ExitPlanMode once you are ready to implement.

## Examples

Plan first:
- "Add sign-in to the app": session or token, where tokens live, how requests are checked.
- "Speed up the database queries": several approaches, profiling needed first, a large effect.
- "Add a dark theme": a decision about how themes work, and it touches many components.
- "Put a delete button on the profile page": looks small, but needs a place, a confirmation, an API call, error handling and state updates.
- "Change how the API reports errors": several files, and the user should agree on the approach.

Just do it:
- "Fix the typo in the README": nothing to plan.
- "Log this variable while debugging": simple and obvious.
- "Which files handle routing?": a question to research, not a change to plan.

## Good to know

- Entering plan mode needs the user's consent.
- When unsure, plan: agreeing first costs less than redoing the work.
- Users value being asked before large changes are made to their code.
