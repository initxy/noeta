Ask the user to resolve a decision only they can make, offering preset choices.

## What it does

Presents 1–3 questions at once. Each question has a short `header` chip, the
`question` text, and optional `choices` (each `{id, label, description}`). When
`allow_freeform` is true (the default) the user may type their own answer instead
of picking a choice. An optional top-level `reason` says why you are asking. The
user's selections are returned so you can proceed.

## When to use

- You are genuinely blocked on a decision that is the user's to make — one you
  cannot settle from the request, the code, or sensible defaults — AND guessing
  wrong would cost more than the round-trip.
- A fork where the options are mutually exclusive and you can enumerate them.

## When NOT to use

- A reasonable default or guess exists: make it, state the assumption, and keep
  working — do not stop to ask.
- Do not ask "should I proceed?" / "is this right?", and do not ask for facts you
  can verify yourself in the codebase or decisions that have a conventional
  default.

## Preconditions

- The `ask_user_question` capability must be enabled for this agent, otherwise
  the tool is not offered.
- At most 3 questions, each with at most 5 choices; a question that supplies no
  choices MUST allow freeform answers.
