---
name: refactor-guide
description: Guidance for safe incremental refactors with a known-exact resource list
version: 2
priority: 50
license: Apache-2.0
disable-model-invocation: false
---

# Refactor Guide

A controlled, authored-for-test skill exercising progressive disclosure
across several bundled Markdown files plus a script.

When refactoring:

1. Read `DEEPENING.md` for the rationale behind small steps.
2. Consult `PATTERNS.md` for concrete before/after transformations.
3. The optional helper `scripts/check.sh` is bundled as a resource; it
   is recorded but never executed by the runtime.
