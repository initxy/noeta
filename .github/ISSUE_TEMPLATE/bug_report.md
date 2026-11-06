---
name: Bug report
about: Report something that behaves incorrectly in noeta
title: "[bug] "
labels: bug
assignees: ''
---

## What happened

A clear, concise description of the incorrect behavior.

## Steps to reproduce

1. …
2. …
3. …

Include the smallest command, program or config that triggers it. If it concerns what the
model was sent or did, the output of `noeta -p --output-format stream-json --verbose` helps.

## Expected behavior

What you expected instead. If Claude Code behaves differently, say how: a difference in what
the model sees is a bug.

## Environment

- OS:
- Python version:
- noeta version / commit: <!-- `noeta --version`, or the commit if built from source -->
- Install: <!-- uv tool install from the repository / a checkout / pip -->
- Provider and model: <!-- anthropic | openai_compat | openai_responses, model id -->
- Store: <!-- sqlite (default) | postgres -->

## Logs

Relevant output or tracebacks. Redact API keys, tokens and sensitive task content.

```
<paste logs here>
```
