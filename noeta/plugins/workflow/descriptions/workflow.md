Run a workflow script that drives many sub-agents in a fixed, scripted way. A workflow runs in the background: this tool returns a run id at once; when the workflow is done, a <task-notification> arrives. Progress shows to the user as it runs.

Call it ONLY after the user has opted into multi-agent orchestration explicitly: a workflow can start dozens of agents and spend a great many tokens, and the user has to ask for that scale, not have it inferred. Opting in is one of:
- The user's prompt contains the word "ultracode" (a system reminder confirms it).
- Ultracode is on for this session (a system reminder confirms it).
- The user asked, in their own words, for a workflow or for orchestrated agents ("fan out agents", "use a workflow", "orchestrate this with subagents", "run a workflow"). A task that would merely benefit from one does not count.
- A skill or slash command the user invoked tells you to call Workflow.
- The user named a particular saved workflow to run.

For anything else, even clearly parallel work, do NOT call this tool: use the Agent tool for single sub-agents, or describe briefly what a workflow would do and roughly what it would cost, and ask whether to run it. Mention that they can say "use a workflow" next time to skip the question.

Size: the setting is "medium", so keep a workflow under 10 agents. It is a guideline; the user's own request may ask for more.

Every script starts with `export const meta = {...}`, a PURE LITERAL (no variables, calls, spreads or interpolation) with the workflow's `name`, a `description` of one line (the user sees it when asked to approve the run) and, if wanted, `phases`: a `{ title, detail? }` for each phase() call, titles matching exactly. Pass the script inline in `script`; do not save it to a file first, and leave `name` unset (`name` picks a saved workflow). It is plain JavaScript, not TypeScript.

Script API:
- `agent(prompt, opts?)` starts a sub-agent and resolves to its final text, or, with `opts.schema` (a JSON Schema whose root is `{type: 'object', properties: {...}}`), to the parsed object. It resolves to null when the agent fails, so filter with `.filter(Boolean)`. `opts`: `label`, `phase` (its progress group), `model`, `effort` ('low' to 'max'), `agentType` (an agent type the Agent tool lists).
- `pipeline(items, stage1, stage2, ...)` runs each item through all stages on its own, with no barrier between stages; each stage gets `(previousResult, item, index)`. A stage that throws turns that item into null. Use it by default for multi-stage work.
- `parallel(thunks)` runs `() => Promise` functions together and waits for all of them (a barrier); a thunk that throws gives null. Use it only when a stage needs every earlier result at once (dedupe, an early exit on zero).
- `phase(title)` starts a progress group; `log(message)` shows the user a progress line; `args` is the tool's `args` input as given.
- At most 16 agents run at once (the rest queue) and 1000 in one run. `Date.now()`, `Math.random()` and `new Date()` without arguments throw (they would break resume); there is no file system or Node.js API. Sub-agents are told their final text is the return value.

The usual shape is a pipeline, where each item's next stage starts as soon as its own previous stage is done:

  export const meta = {
    name: 'audit-services',
    description: 'Audit the services for one kind of risk at a time and double-check each issue',
    phases: [{ title: 'Audit' }, { title: 'Confirm' }],
  }
  const CHECKS = [{id: 'auth', ask: '...'}, {id: 'input', ask: '...'}]
  const out = await pipeline(
    CHECKS,
    c => agent(c.ask, {label: `audit:${c.id}`, phase: 'Audit', schema: ISSUES_SCHEMA}),
    audit => parallel(audit.issues.map(issue => () =>
      agent(`Argue that this is no real problem: ${issue.summary}`, {label: `confirm:${issue.where}`, phase: 'Confirm', schema: CHECK_SCHEMA})
        .then(check => ({...issue, check})))),
  )
  return { real: out.flat().filter(Boolean).filter(i => i.check?.holds) }

Here the issues 'auth' found are being confirmed while 'input' is still being audited.

Each run saves its script to a file and the result gives the path: to change a workflow, edit that file and call Workflow with `scriptPath`. `resumeFromRunId` reruns with the finished results of an earlier run of this task: agent() calls whose prompt and options are unchanged return their earlier result, and only new or edited calls run.
