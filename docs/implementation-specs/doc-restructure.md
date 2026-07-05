# Implementation Spec: Documentation Restructure (Diataxis)

> **Status**: Implemented (landed in `18c0897`; the site later migrated from MkDocs Material to VitePress in `aa8171e`, which supersedes this spec's MkDocs-specific sections — the portal file is now `docs/index.md`, not `docs/README.md`).
> **Scope**: User-facing docs only. 37 ADRs in `docs/adr/` stay put. `README.zh-CN.md` is out of scope (regenerated later from final English README via translate-zh).
> **Goal**: Restructure the flat 7-doc set into a Diataxis-aligned 4-layer architecture (Tutorials / How-to / Concepts / Reference) with clear audience per page.

---

## 1. Final File Tree (Locked)

```
repo-root/
├── README.md                          # Slimmed facade (~700 words)
├── README.zh-CN.md                    # NOT in scope — regenerated later
│
docs/
├── README.md                          # Portal: 4-layer nav + contributor links
│
├── tutorials/                         # Learning-oriented
│   ├── quickstart.md                  # 5-min stub agent + web UI
│   └── first-agent.md                 # 20-min: build your first SDK agent
│
├── how-to/                            # Task-oriented
│   ├── configure-provider.md          # Wire Anthropic / OpenAI provider
│   ├── use-the-coding-agent.md        # Drive noeta-agent for coding tasks
│   ├── build-custom-tools.md          # @tool decorator + input schema + risk
│   ├── spawn-subagents.md             # Fan-out with sub-agents
│   ├── connect-mcp.md                 # Register MCP servers
│   ├── deploy-worker.md               # Stand up WorkerLoop as resident drain
│   └── swap-providers.md              # Switch Anthropic ↔ OpenAI-compatible
│
├── concepts/                          # Understanding-oriented
│   ├── event-sourcing.md              # Why state = fold(log)
│   ├── task-model.md                  # Task primitive + parent-child
│   ├── engine-execution.md            # Engine / Policy / Decision division
│   ├── fold-and-snapshot.md           # Fold mechanism + snapshot accel
│   ├── wake-resume.md                 # Suspend / wake + durable exactly-once
│   ├── guard-observer.md              # Guard (sync veto) vs Observer (async)
│   ├── composer-and-cache.md          # 3-segment context + KV cache
│   └── provider-neutrality.md         # Internal protocol + adapters
│
├── reference/                         # Information-oriented
│   ├── sdk.md                         # noeta.sdk public API
│   ├── noeta-agent.md                 # noeta-agent usage manual
│   ├── http-api.md                    # HTTP route reference (from code)
│   ├── worker-loop.md                 # WorkerLoop primitive ref
│   └── comparison.md                  # noeta vs Claude Agent SDK / Temporal
│
├── architecture/                      # Deep-dive (optional)
│   └── overview.md                    # Top-down architecture walkthrough
│
├── operations/                        # Ops & troubleshooting
│   ├── troubleshooting.md             # Symptom → cause → fix
│   └── limitations.md                 # Known architectural boundaries
│
├── adr/                               # 37 ADRs — UNCHANGED, stay in place
│
├── releasing.md                       # UNCHANGED (release procedure)
│
└── assets/                            # Images, diagrams — UNCHANGED
```

**Files deliberately left alone**: `docs/adr/` (37 ADRs, per locked decision), `docs/releasing.md` (release procedure, contributor-facing), `docs/assets/` (images and SVGs).

---

## 2. Per-File Build Cards

### Root `README.md`

| Field | Value |
|-------|-------|
| **Audience** | First-time visitors evaluating noeta |
| **Duty** | Facade: one-line positioning, Why bullets, screenshots, 3-line quickstart, doc index table, Status, License |
| **Target length** | ~700 words (from 2160) |
| **Source mapping** | Keep: tagline (`README.md:5`), Why bullets (`README.md:30-45`), screenshots (`README.md:18-28`), Status caveat summary (`README.md:309-325`), License (`README.md:326-328`). Drop or move: Quickstart section → `tutorials/quickstart.md`; Three distributions table → `reference/sdk.md` or `concepts/`; SDK "Build your own agent" (`README.md:126-181`) → `tutorials/first-agent.md`; "Noeta vs Claude Agent SDK" (`README.md:183-214`) → `reference/comparison.md`; Installation → `tutorials/quickstart.md`; Repo layout → contributing; Development → contributing |
| **Action** | **Rewrite from existing** |

### `docs/README.md` (Portal)

| Field | Value |
|-------|-------|
| **Audience** | Anyone looking for the right doc page |
| **Duty** | 4-column nav table (Tutorials / How-to / Concepts / Reference) with "Read this when" per entry. Plus a "For contributors" section linking to ADR (`docs/adr/README.md`), `CONTEXT.md`, `AGENTS.md` |
| **Target length** | ~300 words |
| **Source mapping** | Rewrite from current `docs/README.md` structure. Keep the "dividing line" paragraph (`docs/README.md:37-39`). |
| **Action** | **Rewrite from existing** |

---

### `tutorials/quickstart.md`

| Field | Value |
|-------|-------|
| **Audience** | First-time users who want to see noeta running |
| **Duty** | 5-minute guided path: install → run stub agent → open web UI → send a message → view trace. Clear "What you'll do" + time estimate. "Next steps" at the bottom |
| **Target length** | ~500 words |
| **Source mapping** | From `docs/quickstart.md`: Prerequisites (`docs/quickstart.md:7-12`), Install (`docs/quickstart.md:14-31`), Path 1 smoke (`docs/quickstart.md:33-61`), Web UI section (`docs/quickstart.md:98-115`). Drop: Path 2 real provider → `how-to/configure-provider.md`; Coding agent config (`docs/quickstart.md:117-159`) → `reference/noeta-agent.md`. From `README.md`: Quickstart bash snippet (`README.md:52-56`) as teaser |
| **Action** | **Rewrite from existing** |

### `tutorials/first-agent.md`

| Field | Value |
|-------|-------|
| **Audience** | Developers who want to use the SDK, not just the bundled agent |
| **Duty** | 20-minute guided build: define a `@tool`, build `Options`, call `query()`, read the event envelope stream. Uses `FakeLLMProvider` so no API key needed |
| **Target length** | ~800 words |
| **Source mapping** | From `README.md` "Build your own agent" section (`README.md:126-181`) + `examples/sdk_minimal.py` + `examples/custom_tool.py` |
| **Action** | **New** (material from README + examples, needs narrative) |

---

### `how-to/configure-provider.md`

| Field | Value |
|-------|-------|
| **Audience** | Users ready to connect a real LLM |
| **Duty** | Step-by-step: get API key → set env vars → verify with a real run. Covers Anthropic, OpenAI-compatible, OpenAI-Responses |
| **Target length** | ~600 words |
| **Source mapping** | From `docs/quickstart.md` Path 2 (`docs/quickstart.md:63-96`), `README.md` "Use a real model" (`README.md:73-82`), `noeta.agent.backend.lifecycle.build_provider` (`apps/noeta-agent/noeta/agent/backend/lifecycle.py:222-285`) for provider wiring details |
| **Action** | **New** (material from quickstart + lifecycle.py) |

### `how-to/use-the-coding-agent.md`

| Field | Value |
|-------|-------|
| **Audience** | Users of `python -m noeta.agent` for coding tasks |
| **Duty** | How to: start agent → configure workspace → use web chat → manage sessions → use presets → use skills |
| **Target length** | ~700 words |
| **Source mapping** | From `docs/quickstart.md` "Coding agent" (`docs/quickstart.md:117-159`), `docs/noeta-agent.md` Entry point (`docs/noeta-agent.md:11-34`), Agent presets (`docs/noeta-agent.md:65-83`), Skills (`docs/noeta-agent.md:85-105`) |
| **Action** | **New** (material from quickstart + noeta-agent.md) |

### `how-to/build-custom-tools.md`

| Field | Value |
|-------|-------|
| **Audience** | SDK users defining custom tools |
| **Duty** | `@tool` decorator signature, `name` / `version` / `risk_level` / `input_schema`, returning `ToolResult`, calling from `query()` |
| **Target length** | ~500 words |
| **Source mapping** | From `README.md` SDK example `@tool` usage (`README.md:141-148`), `packages/noeta-sdk/noeta/sdk/authoring.py:23` (tool re-export), `packages/noeta-runtime/noeta/tools/decorator.py` (actual decorator) |
| **Action** | **New** |

### `how-to/spawn-subagents.md`

| Field | Value |
|-------|-------|
| **Audience** | SDK users needing parallelism |
| **Duty** | Define `AgentDefinition` children in `Options.agents`, set `allowed_tools`, fan-out, collect results via wake event |
| **Target length** | ~500 words |
| **Source mapping** | From `docs/noeta-agent.md` Sub-agent fan-out (`docs/noeta-agent.md:180-184`), `docs/noeta-architecture-deep-dive.md` §8 multi-agent (`docs/noeta-architecture-deep-dive.md:240-276`), `examples/spawn_subtask.py` |
| **Action** | **New** |

### `how-to/connect-mcp.md`

| Field | Value |
|-------|-------|
| **Audience** | Users wanting MCP tool integrations |
| **Duty** | Register stdio / HTTP MCP servers in config, in-process SDK MCP via `create_sdk_mcp_server` |
| **Target length** | ~500 words |
| **Source mapping** | From `docs/noeta-agent.md` MCP & hooks (`docs/noeta-agent.md:169-178`), `packages/noeta-sdk/noeta/sdk/authoring.py:60-100` (`create_sdk_mcp_server`) |
| **Action** | **New** |

### `how-to/deploy-worker.md`

| Field | Value |
|-------|-------|
| **Audience** | Operators running a resident drain loop |
| **Duty** | Build a `WorkerRuntime` → construct `WorkerLoop` → call `run_forever(install_signals=True)`. Covers sqlite storage, knobs, shutdown |
| **Target length** | ~600 words |
| **Source mapping** | From `docs/daemon.md` "What it is" (`docs/daemon.md:22-89`), "Storage" (`docs/daemon.md:91-102`), "Knobs" (`docs/daemon.md:104-121`), "Lifecycle" (`docs/daemon.md:158-173`) |
| **Action** | **New** (material from daemon.md, reorganized as how-to) |

### `how-to/swap-providers.md`

| Field | Value |
|-------|-------|
| **Audience** | SDK users switching LLM backends |
| **Duty** | Swap `AnthropicProvider` ↔ `OpenAICompatProvider` ↔ `OpenAIResponsesProvider` in `Options` or `Client`. Show before/after |
| **Target length** | ~400 words |
| **Source mapping** | From `README.md` "Provider-neutral" bullet, `examples/swap_provider.py`, `packages/noeta-sdk/noeta/sdk/providers.py` |
| **Action** | **New** |

---

### `concepts/event-sourcing.md`

| Field | Value |
|-------|-------|
| **Audience** | Anyone wanting to understand noeta's core design |
| **Duty** | Explain "state = fold(log)" as the foundational decision. Why this matters (durable by construction, reproducible, replayable) |
| **Target length** | ~500 words |
| **Source mapping** | From `docs/concepts.md` EventLog + Fold sections (`docs/concepts.md:16-29`, `docs/concepts.md:83-89`), `docs/noeta-architecture-deep-dive.md` §2 (`docs/noeta-architecture-deep-dive.md:29-74`) |
| **Action** | **Split from existing** (concepts.md §EventLog + §Fold + deep-dive §2) |

### `concepts/task-model.md`

| Field | Value |
|-------|-------|
| **Audience** | Developers working with noeta tasks |
| **Duty** | Task as the only primitive. `task_id`, status lifecycle (`pending` → `running` → `suspended` → `terminal`), `parent_task_id`, subtask tree |
| **Target length** | ~400 words |
| **Source mapping** | From `docs/concepts.md` Task (`docs/concepts.md:8-14`), deep-dive §8 parent-child (`docs/noeta-architecture-deep-dive.md:256-276`) |
| **Action** | **Split from existing** |

### `concepts/engine-execution.md`

| Field | Value |
|-------|-------|
| **Audience** | Developers wanting to understand the execution model |
| **Duty** | Engine as stateless step driver. `run_one_step` flow. Policy as pure decision function. Decision vocabulary (ToolCalls / Finish / Fail / Spawn / WaitTimer / YieldHuman) |
| **Target length** | ~500 words |
| **Source mapping** | From `docs/concepts.md` Engine (`docs/concepts.md:46-53`) + Policy (`docs/concepts.md:55-61`) + "How a step flows" (`docs/concepts.md:125-152`), deep-dive §4 (`docs/noeta-architecture-deep-dive.md:118-140`) |
| **Action** | **Split from existing** |

### `concepts/fold-and-snapshot.md`

| Field | Value |
|-------|-------|
| **Audience** | Anyone wanting to understand state reconstruction |
| **Duty** | Fold purity (no clock, no IO). Snapshot as accelerator. Two-path iron rule: from-top and snapshot must be byte-equal. Canonical rendering |
| **Target length** | ~500 words |
| **Source mapping** | From `docs/concepts.md` "Fold-based state reconstruction" (`docs/concepts.md:83-89`), deep-dive §3 (`docs/noeta-architecture-deep-dive.md:77-115`) |
| **Action** | **Split from existing** |

### `concepts/wake-resume.md`

| Field | Value |
|-------|-------|
| **Audience** | Developers working with suspended tasks |
| **Duty** | Suspension + typed `WakeCondition`. How wake events match (projection). Durable exactly-once delivery (single-worker scope). What "single-host / single-worker" means |
| **Target length** | ~500 words |
| **Source mapping** | From `docs/concepts.md` Wake-resume (`docs/concepts.md:91-123`). **Drop** the implementation-level detail about `release(consumed_wake_event=…)` and recovery state machines — those go to `architecture/overview.md` |
| **Action** | **Split from existing** (concept-level only; impl detail → architecture) |

### `concepts/guard-observer.md`

| Field | Value |
|-------|-------|
| **Audience** | Extenders and operators |
| **Duty** | Guard = sync veto on hot path (`BudgetGuard`, `PermissionGuard`). Observer = async post-commit subscriber (`AuditObserver`, `MetricsObserver`, `EventFanout`). Why the split |
| **Target length** | ~400 words |
| **Source mapping** | From `docs/concepts.md` Guard/Observer (`docs/concepts.md:70-81`), deep-dive mentions |
| **Action** | **Split from existing** |

### `concepts/composer-and-cache.md`

| Field | Value |
|-------|-------|
| **Audience** | Anyone optimizing agent performance |
| **Duty** | 3-segment context (stable_prefix / semi_stable / dynamic_suffix). Why this enables KV-cache hits. Compaction as event, not in-place edit |
| **Target length** | ~500 words |
| **Source mapping** | From `docs/concepts.md` Composer (`docs/concepts.md:63-68`), deep-dive §5 (`docs/noeta-architecture-deep-dive.md:143-172`) |
| **Action** | **Split from existing** |

### `concepts/provider-neutrality.md`

| Field | Value |
|-------|-------|
| **Audience** | Architects evaluating vendor lock-in |
| **Duty** | Internal neutral protocol. Adapters at the edge. Kernel cannot import vendor (enforced by import-linter). Why recordings are vendor-free |
| **Target length** | ~400 words |
| **Source mapping** | From `docs/concepts.md` implied, deep-dive §6 (`docs/noeta-architecture-deep-dive.md:176-198`), README "Provider-neutral" bullet |
| **Action** | **Split from existing** (deep-dive §6 + README bullet, distilled to concept level) |

---

### `reference/sdk.md`

| Field | Value |
|-------|-------|
| **Audience** | SDK users needing exact API signatures |
| **Duty** | Complete reference for `noeta.sdk` public surface. Each symbol: signature, parameters, return type, exceptions. Organized by category |
| **Target length** | ~2000 words |
| **Source mapping** | From `packages/noeta-sdk/noeta/sdk/__init__.py` `__all__` list (`packages/noeta-sdk/noeta/sdk/__init__.py:108-174`). See §4.1 below for extracted facts |
| **Action** | **New** (extracted from code; existing docs have partial coverage in README "Build your own" + noeta-agent.md) |

### `reference/noeta-agent.md`

| Field | Value |
|-------|-------|
| **Audience** | Users of the bundled coding agent |
| **Duty** | Usage manual: entry point, env config, tool catalog (with examples), permission model, presets, skills, MCP config. NOT the HTTP API — that's `http-api.md` |
| **Target length** | ~1200 words |
| **Source mapping** | From `docs/noeta-agent.md`: Entry point (`docs/noeta-agent.md:11-34`), Tool surface (`docs/noeta-agent.md:36-63`), Agent presets (`docs/noeta-agent.md:65-83`), Skills (`docs/noeta-agent.md:85-105`), Write & shell safety (`docs/noeta-agent.md:107-128`), MCP & hooks (`docs/noeta-agent.md:169-178`), Sub-agent (`docs/noeta-agent.md:180-184`). Drop: HTTP surface table → `http-api.md`. Drop: "thin map for agents" framing |
| **Action** | **Rewrite from existing** + split HTTP routes to http-api.md |

### `reference/http-api.md`

| Field | Value |
|-------|-------|
| **Audience** | Developers integrating with noeta-agent over HTTP |
| **Duty** | Every route: method, path, purpose, request body shape, response shape, error codes. **Must be extracted from actual code, not existing docs** — see §4.2 for the verified route list and discrepancy analysis |
| **Target length** | ~1500 words |
| **Source mapping** | From `apps/noeta-agent/noeta/agent/backend/` — `task_protocol.py:188-197`, `read_views.py:212-213`, `resource_services.py:168-170`, `workspace_service.py:84-86`, `mcp_service.py:226-233`, `app.py:302-303` (health). Also `app.py:262-292` for static/preview routes |
| **Action** | **New** (from code; existing `noeta-agent.md` HTTP table is INACCURATE — see §4.2) |

### `reference/worker-loop.md`

| Field | Value |
|-------|-------|
| **Audience** | Embedders running a resident drain |
| **Duty** | `WorkerLoop` constructor params, `run_forever`, `stop`, `tick`, `abandoned` property. `WorkerRuntime` protocol. `install_stop_signals`. Exception policy |
| **Target length** | ~800 words |
| **Source mapping** | From `docs/daemon.md` (`docs/daemon.md:22-295`) + `packages/noeta-runtime/noeta/runtime/worker.py` `WorkerLoop.__init__` (`worker.py:775-792`), `WorkerLoop.stop` (`worker.py:841-844`), `WorkerLoop.tick` (`worker.py:864-880`), `WorkerLoop.run_forever` (`worker.py:1093-1115`), `WorkerRuntime` protocol (`worker.py:205-234`). See §4.3 for full extracted facts. Drop: "The chat server" section (`docs/daemon.md:123-156`) → belongs in `reference/noeta-agent.md` |
| **Action** | **Rewrite from existing** (daemon.md minus chat server, plus code-verified API) |

### `reference/comparison.md`

| Field | Value |
|-------|-------|
| **Audience** | Technical decision-makers evaluating noeta vs alternatives |
| **Duty** | noeta vs Claude Agent SDK (merged from both sources, deduplicated). Optional: noeta vs Temporal brief comparison |
| **Target length** | ~1000 words |
| **Source mapping** | From `README.md` "Noeta vs Claude Agent SDK" (`README.md:183-214`) + `docs/noeta-architecture-deep-dive.md` §10 (`docs/noeta-architecture-deep-dive.md:306-331`). See §4.4 for overlap analysis |
| **Action** | **Merge from existing** (two sources, deduplicated) |

---

### `architecture/overview.md`

| Field | Value |
|-------|-------|
| **Audience** | Engineers who want the full architecture story |
| **Duty** | Top-down walkthrough: 3-package layout → core design decision (fold) → engine loop → context assembly → provider neutrality → SDK surface → agent layer → distribution. Reference-style prose, not blog-narrative |
| **Target length** | ~2500 words (from 5330) |
| **Source mapping** | From `docs/noeta-architecture-deep-dive.md`: §1 (`docs/noeta-architecture-deep-dive.md:5-26`), §3 (`docs/noeta-architecture-deep-dive.md:77-115` — fold purity), §7 (`docs/noeta-architecture-deep-dive.md:202-236` — SDK), §8 (`docs/noeta-architecture-deep-dive.md:240-276` — agent layer), §9 (`docs/noeta-architecture-deep-dive.md:280-302` — distribution). Drop: §2/§4/§5/§6 → moved to concepts/. Drop: §10 → `reference/comparison.md` |
| **Action** | **Split + rewrite from existing** |

---

### `operations/troubleshooting.md`

| Field | Value |
|-------|-------|
| **Audience** | Users encountering errors |
| **Duty** | Structured entries: Symptom → Cause → Resolution. Covers: missing API key, budget exhaustion, permission denial, suspended task won't wake |
| **Target length** | ~600 words |
| **Source mapping** | From `docs/failure-modes.md`: Missing API key (`docs/failure-modes.md:5-22`), Budget exhaustion (`docs/failure-modes.md:24-55`), Permission denial (`docs/failure-modes.md:57-66`). Drop: architecture-level H2 / WorkerLoop sections → `operations/limitations.md` + `reference/worker-loop.md`. Drop: Engine 500-line budget → contributing |
| **Action** | **Split from existing** (user-facing errors only) |

### `operations/limitations.md`

| Field | Value |
|-------|-------|
| **Audience** | Operators and architects evaluating production readiness |
| **Duty** | Honest boundary list: single-host only, single-worker, partial-step-orphan, bounded shutdown, heartbeat cap. Each: what it means, when you hit it, workaround if any |
| **Target length** | ~700 words |
| **Source mapping** | From `docs/failure-modes.md` "Durable exactly-once wake" (`docs/failure-modes.md:68-95`) + "Resident worker loop" (`docs/failure-modes.md:97-160`), `docs/daemon.md` Limitations (`docs/daemon.md:201-277`), `README.md` Status (`README.md:309-325`) |
| **Action** | **Merge from existing** (failure-modes.md arch limits + daemon.md Limitations + README Status, deduplicated) |

---

## 3. Link Migration Plan

### 3.1 Cross-document references that will break

All files that reference moving docs, verified by `grep -rn`:

| Source file | Line | Current link | Must change to |
|-------------|------|--------------|----------------|
| `README.md` | 218 | `docs/quickstart.md` | `docs/tutorials/quickstart.md` |
| `README.md` | 219 | `docs/concepts.md` | `docs/README.md` (portal) or `docs/concepts/event-sourcing.md` |
| `README.md` | 220 | `docs/noeta-agent.md` | `docs/reference/noeta-agent.md` |
| `README.md` | 221 | `docs/noeta-architecture-deep-dive.md` | `docs/architecture/overview.md` |
| `README.md` | 222 | `docs/failure-modes.md` | `docs/operations/troubleshooting.md` |
| `README.md` | 320 | `docs/failure-modes.md` | `docs/operations/limitations.md` |
| `README.zh-CN.md` | 197-201, 295 | various `docs/*.md` | **Out of scope** — regenerated from final English |
| `docs/README.md` | 14 | `quickstart.md` | `tutorials/quickstart.md` |
| `docs/README.md` | 15 | `concepts.md` | portal should link to `concepts/event-sourcing.md` as entry |
| `docs/README.md` | 16 | `noeta-agent.md` | `reference/noeta-agent.md` |
| `docs/README.md` | 25 | `noeta-architecture-deep-dive.md` | `architecture/overview.md` |
| `docs/README.md` | 26 | `failure-modes.md` | `operations/troubleshooting.md` |
| `docs/README.md` | 27 | `daemon.md` | `reference/worker-loop.md` |
| `docs/concepts.md` | 123 | `failure-modes.md` | `../operations/limitations.md` |
| `docs/quickstart.md` | 158 | `noeta-agent.md` | `../reference/noeta-agent.md` |
| `docs/quickstart.md` | 163 | `concepts.md` | `../concepts/event-sourcing.md` |
| `docs/quickstart.md` | 165 | `noeta-agent.md` | `../reference/noeta-agent.md` |
| `docs/quickstart.md` | 167 | `failure-modes.md` | `../operations/troubleshooting.md` |
| `docs/quickstart.md` | 169 | `daemon.md` | `../reference/worker-loop.md` |
| `docs/failure-modes.md` | 108 | `daemon.md` | `../reference/worker-loop.md` |
| `docs/failure-modes.md` | 144 | `daemon.md#crash-recovery-...` | `../reference/worker-loop.md#...` (or limitations) |
| `docs/daemon.md` | 236 | `failure-modes.md` | `../operations/troubleshooting.md` |
| `docs/daemon.md` | 281 | `failure-modes.md` | `../operations/troubleshooting.md` |
| `docs/daemon.md` | 283 | `noeta-agent.md` | `../reference/noeta-agent.md` |
| `docs/daemon.md` | 285 | `concepts.md` | `../concepts/wake-resume.md` (or portal) |

**No references found in**: `CONTEXT.md`, `AGENTS.md`, `CONTRIBUTING.md`, `examples/**/README.md`.

### 3.2 Code / CI that hardcodes doc paths — CONFIRMED

| File | Line | What it references | Fix needed |
|------|------|--------------------|------------|
| `tests/test_docs_codeblocks.py` | 54 | `_REPO_ROOT / "docs" / "quickstart.md"` in `_RUNNABLE_MD_FILES` tuple | **CONFIRMED: Must update** to `_REPO_ROOT / "docs" / "tutorials" / "quickstart.md"` |

**About adding `tutorials/first-agent.md` to the test**: The test (`tests/test_docs_codeblocks.py:52-55`) uses an explicit allowlist `_RUNNABLE_MD_FILES`, not a glob. If `tutorials/first-agent.md` contains `<!-- runnable: smoke -->` blocks, it must be explicitly added to this tuple for the test to execute them. Otherwise the blocks are silently ignored (which is also fine if first-agent doesn't need runnable blocks). **Recommendation**: Add it if the tutorial includes a runnable Python smoke block.

**Additional test gates that scan docs** (from `tests/test_docs_codeblocks.py`):
- `test_docs_dont_promise_pypi_install_paths` (`tests/test_docs_codeblocks.py:121-159`) scans `docs/*.md` (non-recursive glob) + README. **Risk**: After moving files into `docs/tutorials/*.md` etc., the glob `docs/*.md` will no longer find them. **Fix needed**: Change `_REPO_ROOT.glob("docs/*.md")` to `_REPO_ROOT.glob("docs/**/*.md")` (recursive) or explicitly list the new paths. This test ensures docs don't falsely promise PyPI install paths.
- `test_no_pre_h2_wake_residue_in_user_docs` (`tests/test_docs_codeblocks.py:172-207`) also scans `docs/*.md` non-recursively. **Same fix needed**: expand to recursive glob.

### 3.3 MkDocs / docs site status

**CONFIRMED**: There is **no** `mkdocs.yml` or `docs.yml` in this worktree (verified with `ls` + `find -name '*.yml'`). The only YAML files are `.github/workflows/release.yml`, `.github/workflows/ci.yml`, `.github/ISSUE_TEMPLATE/config.yml`.

> ⚠️ **RISK (from memory)**: The project memory references a bilingual MkDocs site at `initxy.github.io/noeta` deploying via `docs.yml`. If that nav config lives on another branch (e.g. `gh-pages`), moving files into `tutorials/`, `how-to/`, `concepts/`, `reference/`, `architecture/`, `operations/` will **break the nav** until it's updated. This is a known dependency — flag for the maintainer. It does not block this spec (the nav update is a separate concern on that branch).

---

## 4. Reference Page Technical Facts (Code-Extracted)

### 4.1 `reference/sdk.md` — `noeta.sdk` Public Surface

**CONFIRMED**: Source of truth is `packages/noeta-sdk/noeta/sdk/__init__.py` `__all__` at lines 108-174.

**Category: Client verbs**
- `query(options, goal, *, provider=None, workspace_dir=None, model=None, images=())` → `QueryResult` — `packages/noeta-sdk/noeta/client/client.py:984-1022`
- `Client(options, *, provider=None, workspace_dir=None, model=None, multi_turn=True, host_config=None, allowed_models=None)` — `client/client.py:122-330`
  - `client.start(*, goal, agent=None, model_selector=None, images=(), permission_mode=None, enabled_mcp=(), workspace_dir=None, effort=None)` → outcome — `client/client.py:391-437`
  - `client.send_goal(task_id, *, goal, model_selector=None, images=(), permission_mode=None, enabled_mcp=(), effort=None)` → outcome — `client/client.py:439-472`
  - `client.approve(task_id, *, call_id, reason=None, resolver="client")` → outcome — `client/client.py:474-485`
  - `client.deny(task_id, *, call_id, reason=None, resolver="client")` → outcome — `client/client.py:487-498`
  - `client.answer(task_id, *, question_id, answers, answered_by="client")` → outcome — `client/client.py:500-514`
  - `client.cancel(task_id, *, reason="cancelled", cascade=False)` → outcome — `client/client.py:623-633`
  - `client.close(task_id, *, closed_by="user", reason=None)` → outcome — `client/client.py:635-645`
  - `client.reopen(task_id, *, reopened_by="user", reason=None)` → outcome — `client/client.py:647-657`
  - `client.events(task_id)` → `list[EventEnvelope]` — `client/client.py:671-673`
  - `client.messages(task_id)` → `list[ViewItem]` — `client/client.py:675-683`
  - `client.delete_task(task_id)` → `dict` — `client/client.py:704-777`
  - `client.subscribe(callback)` → unsubscribe callable — `client/client.py:812-820`
  - `client.shutdown()` → None — `client/client.py:822-840`
- `QueryResult` — `client/client.py:881-931`. Subclass of `list[EventEnvelope]`. Has `.task_id` (str), `.messages()` → `list[ViewItem]`, `.answer()` → Any (raises `QueryFailedError` if not completed)
- `QueryFailedError` — `client/client.py:848-878`. Subclass of `CodedError` with `code="query_failed"`. Attributes: `task_id`, `status`, `reason`, `retryable`

**Category: Recipe (Options)**
- `Options` — `packages/noeta-sdk/noeta/client/options.py:196-358`. Frozen dataclass. Key fields:
  - `system_prompt: str | SystemPromptPreset` (required)
  - `name: str = "main"`
  - `skills: tuple[str, ...] = ()`
  - `budget: BudgetSpec | None = None`
  - `capabilities: Capabilities | None = None`
  - `model: str | None = None`
  - `provider: LLMProvider | None = None` (wiring, not identity)
  - `agents: Mapping[str, AgentDefinition] = {}`
  - `allowed_tools: tuple[Any, ...] | None = None` (None = all 11 builtins)
  - `disallowed_tools: tuple[str, ...] = ()`
  - `permission_mode: str = "default"` (legal values: `"default"`, `"acceptEdits"`, `"bypassPermissions"`)
  - `max_turns: int | None = None`
  - `cwd: Any = None` (wiring)
  - `can_use_tool: Any = None` (wiring callback)
  - `output_schema: Mapping | None = None` (wiring)
  - `thinking: str | None = None` (wiring; `"adaptive"` or `"disabled"`)
  - `effort: str | None = None` (wiring; `"low"`/`"medium"`/`"high"`/`"xhigh"`/`"max"`)
  - `policy: Any = None` (extension — must expose `.ref` → `ComponentRef`)
  - `guards: tuple[Guard, ...] = ()` (extension, wiring)
  - `observers: tuple[Subscriber, ...] = ()` (extension, wiring)
  - `content_channels: tuple[ContentKindSpec, ...] = ()` (extension)
  - `mcp_servers: tuple[Any, ...] = ()` (extension)
- `AgentDefinition` — `client/options.py:120-170`. Frozen dataclass. Fields: `description` (required), `prompt` (required), `tools`, `model`, `capabilities`, `metadata`
- `SystemPromptPreset` — `client/options.py:100-117`. `preset: str = "main"`, `append: str | None = None`
- `compile_options(options)` → `(AgentSpec, tuple[AgentSpec, ...])` — `client/options.py:514-660`
- `register_preset_prompt(name, prompt)` — `client/options.py:84-92`

**Category: Authoring**
- `tool` decorator — re-exported from `noeta.tools.decorator`. `packages/noeta-sdk/noeta/sdk/authoring.py:23`
- `DecoratedTool` — the decorated function type
- `create_sdk_mcp_server(name, version="1.0.0", tools=())` → `SdkMcpServer` — `sdk/authoring.py:60-100`
- `SdkMcpServer` — frozen dataclass: `name`, `version`, `tools` — `sdk/authoring.py:34-57`

**Category: Message projection**
- `as_messages(envelopes, content_store)` → `list[ViewItem]`
- `AssistantMessage`, `UserMessage`, `ToolUse`, `ToolResultView`, `Result` — message view types

**Category: Host config**
- `HostConfig` — from `noeta.client.host_config`. Storage triple + app_gateway + MCP resolver + write_mode

**Category: Extension interfaces**
- `Tool`, `ToolContext`, `ToolResult` — from `noeta.protocols.tool`
- `LLMProvider` — from `noeta.protocols.messages`
- `Policy` — from `noeta.protocols.policy`
- `Guard`, `GuardContext`, `ProposedAction`, `VerdictResult` — from `noeta.protocols.hooks`
- `Observer` (= `Subscriber`) — from `noeta.protocols.event_log`
- `ContentKindSpec` — from `noeta.context.content_channel`
- `Decision` — from `noeta.protocols.decisions`
- `StepContext` — from `noeta.protocols.step_context`
- `View` — from `noeta.protocols.view`

**Category: Errors (typed/coded)**
- `CodedError` — base, `.code` attribute
- `QueryFailedError` — `code="query_failed"`
- `ModelSelectorError` — `code="model_selector_rejected"` → HTTP 400
- `ProviderSelectorError` — `code="provider_selector_rejected"` → HTTP 400
- `NotResumableError` — `code="not_resumable"` → HTTP 409
- `TaskAlreadyTerminalError` — `code="task_already_terminal"` → HTTP 409
- `UnsupportedSubtaskSuspend` — `code="unsupported_subtask_suspend"` → HTTP 409

**Category: Capability projections**
- `permission_modes` — frozenset of valid mode strings
- `effort_modes` — frozenset: `{"low", "medium", "high", "xhigh", "max"}`
- `model_capabilities` — per-model capability info

### 4.2 `reference/http-api.md` — Actual HTTP Routes

**IMPORTANT**: The existing `docs/noeta-agent.md` HTTP surface table (`docs/noeta-agent.md:138-163`) does NOT match actual code. Several routes in the docs don't exist in code, and several in code aren't in the docs.

**Verified routes from `router.add()` calls**:

| Method | Path | Purpose | Source file:line |
|--------|------|---------|-----------------|
| GET | `/health` | Liveness probe | `app.py:302` |
| GET | `/` | Redirect to `/chat` | `app.py:273` |
| GET | `/chat`, `/trace`, `/assets/*`, etc. | Static SPA assets | `app.py:262-292` |
| GET | `/preview/*` | HTML app preview gateway (prefix-routed) | `app.py:224-252` |
| GET | `/stream` | SSE live event stream | `task_protocol.py:188` |
| POST | `/tasks` | Create a task (goal + agent + optional model selector) | `task_protocol.py:189` |
| POST | `/tasks/{id}/messages` | Append a follow-up goal | `task_protocol.py:190` |
| POST | `/tasks/{id}/approve` | Approve a gated tool call | `task_protocol.py:191` |
| POST | `/tasks/{id}/deny` | Deny a gated tool call | `task_protocol.py:192` |
| POST | `/tasks/{id}/answer` | Answer a structured user question | `task_protocol.py:193` |
| POST | `/tasks/{id}/cancel` | Cancel a task | `task_protocol.py:194` |
| POST | `/tasks/{id}/close` | Close / archive a task | `task_protocol.py:195` |
| POST | `/tasks/{id}/reopen` | Reopen a closed task | `task_protocol.py:196` |
| DELETE | `/tasks/{id}` | Hard-delete a task + subtask tree | `task_protocol.py:197` |
| GET | `/capabilities` | Agents / models / providers / MCP / workspace probe | `read_views.py:212` |
| GET | `/tasks` | Task list | `read_views.py:213` |
| GET | `/content/{hash}` | Decoded content-ref body | `resource_services.py:168` |
| GET | `/files` | Workspace file tree | `resource_services.py:169` |
| GET | `/file` | Single-file preview | `resource_services.py:170` |
| GET | `/workspaces` | List workspace registries | `workspace_service.py:84` |
| POST | `/workspaces` | Create a workspace | `workspace_service.py:85` |
| DELETE | `/workspaces/{id}` | Delete a workspace | `workspace_service.py:86` |
| GET | `/mcp/servers` | List MCP server registrations | `mcp_service.py:230` |
| POST | `/mcp/servers` | Create an MCP server | `mcp_service.py:231` |
| PUT | `/mcp/servers/{alias}` | Update an MCP server | `mcp_service.py:232` |
| DELETE | `/mcp/servers/{alias}` | Delete an MCP server | `mcp_service.py:233` |
| GET | `/mcp/servers/{alias}/tools` | List tools for an MCP server | `mcp_service.py:226` |
| PUT | `/mcp/servers/{alias}/tools` | Set tools for an MCP server | `mcp_service.py:227` |
| GET | `/mcp/servers/{alias}/prompts` | List prompts for an MCP server | `mcp_service.py:228` |
| GET | `/mcp/servers/{alias}/resources` | List resources for an MCP server | `mcp_service.py:229` |

**Discrepancy analysis: existing docs table vs actual code**

Routes in `docs/noeta-agent.md:138-163` that **DO NOT EXIST** in code:
| Claimed in docs | Actual situation |
|-----------------|-------------------|
| `GET /skills` | **Not found.** No `/skills` route in code. |
| `GET /tasks/{id}` | **Not found.** Code has `GET /tasks` (list) but no per-task detail route. |
| `GET /tasks/{id}/events` | **Not found.** No envelope history route. |
| `GET /tasks/{id}/context` | **Not found.** Context view exists in read_models but not as an HTTP route. |
| `GET /tasks/{id}/files` · `/tasks/{id}/file` | **Wrong path.** Code has `GET /files` and `GET /file` (flat, not under `/tasks/{id}/`). |
| `GET /tasks/{id}/artifacts/{hash}` | **Not found.** No artifacts route. |
| `GET /tasks/{id}/images/{hash}` | **Not found.** No images route. |
| `GET /tasks/{id}/messages/{hash}` | **Not found.** No per-message prose route. |
| `GET /tasks/{id}/content/{hash}` | **Wrong path.** Code has `GET /content/{hash}` (flat, not task-scoped). |
| `GET /events` | **Wrong name.** Code uses `GET /stream` for SSE. |
| `POST /tasks/{id}/goals` | **Wrong path.** Code uses `POST /tasks/{id}/messages`. |
| `POST /tasks/{id}/approvals` · `/answers` | **Wrong paths.** Code uses separate `POST /tasks/{id}/approve`, `POST /tasks/{id}/deny`, `POST /tasks/{id}/answer` (singular, not plural). |
| `POST /tasks/{id}/rewind` | **Not found.** No rewind route. |
| `POST /tasks/{id}/resume` | **Not found.** No resume route. |
| `GET /mcp-servers` | **Wrong path.** Code uses `/mcp/servers` (slash, not hyphen). |
| `GET /workspaces/{id}/files` | **Not found.** No per-workspace files route. |
| `POST /workspaces[...]` · `PUT /workspaces[...]` | **Partially wrong.** Code has `POST /workspaces` and `DELETE /workspaces/{id}`, but NO `PUT /workspaces[...]`. |

Routes in code that are **MISSING from existing docs**:
| Route | Notes |
|-------|-------|
| `GET /health` | Liveness probe |
| `GET /stream` | SSE stream (docs called it `/events`) |
| `PUT /mcp/servers/{alias}/tools` | Set tools for MCP server |
| `GET /mcp/servers/{alias}/prompts` | List prompts |
| `GET /mcp/servers/{alias}/resources` | List resources |
| `POST /workspaces` | Create workspace |
| `DELETE /workspaces/{id}` | Delete workspace |
| `POST /tasks/{id}/approve` (separate from deny) | Docs lumped them as `approvals` |

**Bottom line for writer**: The existing `docs/noeta-agent.md` HTTP table (`docs/noeta-agent.md:138-163`) is **unreliable**. The `reference/http-api.md` page must be written from the code-verified table above, not from the existing docs.

### 4.3 `reference/worker-loop.md` — WorkerLoop Facts

Source: `packages/noeta-runtime/noeta/runtime/worker.py`

**WorkerLoop class** — `worker.py:752-1115`

**Constructor** (`worker.py:775-792`):
```python
WorkerLoop(
    rt: WorkerRuntime,
    *,
    worker_id: str = "noeta-worker",
    lease_seconds: float = 600.0,
    poll_interval: float = 0.5,
    heartbeat_interval: float = 30.0,
    stale_sweep_interval: float = 10.0,
    timer_poll_interval: float = 1.0,
    shutdown_grace_s: Optional[float] = 30.0,  # DEFAULT_SHUTDOWN_GRACE_S
    sleep: Optional[Callable[[float], None]] = None,
    clock: Optional[Callable[[], float]] = None,
    now_fn: Optional[Callable[[], float]] = None,
    heartbeat_wait: Optional[Callable[[float], bool]] = None,
    reliability_sink: Optional[ReliabilitySink] = None,
    step_poll_s: float = 0.05,
)
```

**Public methods**:
- `stop()` → `None` — `worker.py:841-844`. Signal loop to stop after current iteration.
- `tick()` → `bool` — `worker.py:864-880`. Lease one ready task and advance it one step. Returns `True` if a task was processed, `False` if queue empty.
- `maybe_sweep()` → `bool` — `worker.py:882-904`. Run `requeue_stale()` if interval elapsed. Returns `True` if sweep ran.
- `maybe_poll_timers()` → `bool` — `worker.py:906-938`. Run `fire_due_timers()` if interval elapsed.
- `run_forever(*, install_signals: bool = False)` → `None` — `worker.py:1093-1115`. Drive tasks until `stop()` called. Runs stale-sweep + timer-poll each iteration. Sleeps `poll_interval` when queue empty.

**Properties**:
- `running: bool` — `worker.py:846-848`. Whether loop is still running.
- `abandoned: bool` — `worker.py:850-856`. `True` if a shutdown grace elapsed with a step still in flight. Host MUST exit the process.

**WorkerRuntime protocol** — `worker.py:205-234`:
```python
class WorkerRuntime(Protocol):
    @property
    def engine(self) -> Any: ...
    @property
    def event_log(self) -> Any: ...
    @property
    def content_store(self) -> Any: ...
    @property
    def dispatcher(self) -> Any: ...
```
Optional: `resolve_engine(task)` method for multi-agent hosts — `worker.py:237-252`.

**Helper functions**:
- `install_stop_signals(loop: WorkerLoop)` → `Callable[[], None]` — `worker.py:1118-1149`. Wire SIGTERM/SIGINT to `loop.stop()`. Returns a restore callable.
- `keep_lease_alive(dispatcher, lease, *, interval, lease_seconds, reliability_sink)` → context manager — `worker.py:708-734`. Renew lease for duration of a synchronously-driven step.
- `run_leased_task(rt, lease, *, prelude=None, next_goal_handle=None)` → `WorkerOutcome` — `worker.py:390-417`. The canonical 3-state resume machine (`"woken"` / `"drained"` / `"skipped"`).
- `resolve_engine(rt, task)` → Engine — `worker.py:237-252`. Per-task engine resolver seam.

**WorkerOutcome type** — `worker.py:148-150`:
```python
WorkerOutcome = Literal["woken", "drained", "skipped", "cancelled", "stopped"]
```

**Exception policy** (from `worker.py:755-767` docstring):
- `InvalidLease` → log + continue (do NOT release/fail)
- Any other exception → `dispatcher.fail(lease_id, retryable=True, reason=...)`: bounded retry then terminal
- If `fail()` itself raises → log + continue
- Loop always proceeds to next task

**ReliabilityEvent kinds** — `worker.py:110-117`:
`"stale_requeued"`, `"suspended_without_wake"`, `"step_failed_retryable"`, `"heartbeat_invalid_lease"`, `"shutdown_abandoned"`, `"timers_fired"`

**Exceptions defined in worker.py**:
- `WakeRecoveryError` — `worker.py:153-157`. Woken lease's wake cannot be reconciled.
- `PartialStepOrphan` — `worker.py:160-164`. After durable `TaskWoken`, step crashed mid-flight with partial events.

### 4.4 `reference/comparison.md` — Overlap Analysis

**Source A**: `README.md:183-214` ("Noeta vs the Claude Agent SDK — a server-side view")
**Source B**: `docs/noeta-architecture-deep-dive.md:306-331` (§10 "Contrast with the Claude Agent SDK")

**Dimensions covered in BOTH sources (duplicates to merge)**:
| Dimension | Source A location | Source B location |
|-----------|-------------------|-------------------|
| State / session model | `README.md:196` "Session JSONL" vs "state = fold(events)" | `deep-dive.md:312` "session JSONL" vs "event-sourced log + fold" |
| Recovery / resume | `README.md:196` "resume replays the conversation" | `deep-dive.md:313` "resume / fork by session id" |
| Compaction | `README.md:198` "Auto-summary, irreversible" vs "recorded event, original history never scrubbed" | `deep-dive.md:314` "auto-summary, irreversible" vs "compaction is an event" |
| Provider | `README.md:199` "configures multiple backends, but Anthropic-centric" vs "vendor-neutral internal protocol" | `deep-dive.md:315` "multiple backends (Anthropic / Bedrock / Vertex / Azure)" vs "neutral internal protocol" |
| Subagents | `README.md:200` "A single in-process query / client" vs "lease + durable-log queue substrate" | `deep-dive.md:319` "agents definitions, output returns to parent" vs "subtasks are independent event-sourced tasks" |
| Scheduling / distribution | `README.md:200` (same row as subagents) | `deep-dive.md:320` "a single query / client in-process" |

**Unique to Source A** (README):
- "Who owns the execution substrate" row (`README.md:195`) — you host the loop vs you own the infrastructure
- "Suspend / resume / exactly-once wake" row (`README.md:197`) — resume/fork by session id vs first-class durable wake
- "When each wins" paragraph (`README.md:202-207`) — guidance on when to reach for which
- "Honest server-side caveats" (`README.md:209-214`) — pre-1.0, single-host, smaller ecosystem

**Unique to Source B** (deep-dive §10):
- Tools row (`deep-dive.md:316`) — builtin tools + @tool + in-process SDK MCP server vs builtin + @tool(version/risk) + MCP(stdio/HTTP)
- Permissions row (`deep-dive.md:317`) — permission_mode + canUseTool + hook chain vs permission_mode + guards
- Extension row (`deep-dive.md:318`) — hooks, imperative interception (Pre/PostToolUse) vs five extension seams + single-writer constraint
- Shape row (`deep-dive.md:321`) — TS / Python library sending straight to Claude API vs three packages runtime/sdk/app
- "Three differences spelled out" (`deep-dive.md:323-331`):
  1. Shape of ground truth (conversation recording vs state machine ledger)
  2. Reversibility of compaction (summary displaces content vs summary is a layer over original)
  3. Provider boundary (Anthropic-centric vs neutral)

**Merge instructions for writer**:
1. Use Source A's table structure (it has "Server-side concern" framing which is clearer)
2. Add the 3 rows unique to Source B (tools, permissions, extension)
3. Keep Source A's "When each wins" guidance
4. Keep Source A's "Honest caveats" as a separate callout box
5. Use Source B's "Three differences spelled out" as deeper explanatory text below the table (narrative depth that Source A lacks)
6. Deduplicate the state/recovery/compaction/provider rows — they say the same thing in both

---

## 5. Writer Batch Sequencing & Acceptance Criteria

### Rationale for batch order

The batches are ordered by dependency: the portal (batch 1) must point to correct paths; concepts (batch 2) are referenced by everything else; reference (batch 3) is referenced by tutorials and how-tos; tutorials/how-tos (batch 4) are the "top layer" that users encounter first; operations (batch 5) can be done last since it's reference material.

### Batch 1: Foundation — README + Portal + Link Migration + Test Fixes

**Files**: `README.md` (slim), `docs/README.md` (portal), all link updates from §3.1, `tests/test_docs_codeblocks.py` path fix

**Acceptance criteria**:
- [ ] `README.md` is ≤800 words
- [ ] `docs/README.md` shows 4-column nav (Tutorials / How-to / Concepts / Reference)
- [ ] All cross-doc links from §3.1 updated to new paths
- [ ] `tests/test_docs_codeblocks.py:54` updated to `docs/tutorials/quickstart.md`
- [ ] `tests/test_docs_codeblocks.py` doc-scan globs (lines 138, 181) changed from `docs/*.md` to `docs/**/*.md`
- [ ] `pytest tests/test_docs_codeblocks.py` passes
- [ ] No broken internal links (grep for old paths returns empty)

### Batch 2: Concepts + Architecture

**Files**: `concepts/event-sourcing.md`, `concepts/task-model.md`, `concepts/engine-execution.md`, `concepts/fold-and-snapshot.md`, `concepts/wake-resume.md`, `concepts/guard-observer.md`, `concepts/composer-and-cache.md`, `concepts/provider-neutrality.md`, `architecture/overview.md`

**Acceptance criteria**:
- [ ] 8 concept pages written, each ≤500 words
- [ ] `architecture/overview.md` ≤2500 words
- [ ] No ADR references in concept pages (use plain English instead)
- [ ] No implementation-level detail in concept pages (e.g. no `release(consumed_wake_event=…)`)
- [ ] Concept pages cross-reference each other where relevant
- [ ] Architecture page links to concept pages for "what is X" questions
- [ ] No duplicate content between concept pages and architecture page
- [ ] Old `docs/concepts.md` and `docs/noeta-architecture-deep-dive.md` can be deleted (or kept as redirect stubs — maintainer decision)

### Batch 3: Reference

**Files**: `reference/sdk.md`, `reference/noeta-agent.md`, `reference/http-api.md`, `reference/worker-loop.md`, `reference/comparison.md`

**Acceptance criteria**:
- [ ] `reference/sdk.md` covers all symbols from `__all__` in `packages/noeta-sdk/noeta/sdk/__init__.py:108-174`
- [ ] `reference/http-api.md` routes match the code-verified table in §4.2 (NOT the old docs table)
- [ ] `reference/worker-loop.md` constructor params match `worker.py:775-792`
- [ ] `reference/comparison.md` deduplicated per §4.4 instructions
- [ ] `reference/noeta-agent.md` has no HTTP routes (those are in http-api.md)
- [ ] Every code symbol in reference pages has a `file:line` citation
- [ ] Old `docs/noeta-agent.md` and `docs/daemon.md` can be deleted

### Batch 4: Tutorials + How-to

**Files**: `tutorials/quickstart.md`, `tutorials/first-agent.md`, `how-to/configure-provider.md`, `how-to/use-the-coding-agent.md`, `how-to/build-custom-tools.md`, `how-to/spawn-subagents.md`, `how-to/connect-mcp.md`, `how-to/deploy-worker.md`, `how-to/swap-providers.md`

**Acceptance criteria**:
- [ ] `tutorials/quickstart.md` has "What you'll do" + "~5 minutes" at top
- [ ] `tutorials/quickstart.md` contains at least one `<!-- runnable: smoke -->` Python block (so the test from batch 1 covers it)
- [ ] Each how-to has a clear "Goal" / "Before you start" / "Steps" structure
- [ ] Tutorials and how-tos link to concept and reference pages for deeper reading
- [ ] `tutorials/first-agent.md` works with `FakeLLMProvider` (no API key needed)
- [ ] Old `docs/quickstart.md` can be deleted

### Batch 5: Operations

**Files**: `operations/troubleshooting.md`, `operations/limitations.md`

**Acceptance criteria**:
- [ ] `troubleshooting.md` entries follow "Symptom / Cause / Resolution" format
- [ ] `limitations.md` each entry has: what it means, when you hit it, workaround if any
- [ ] No duplicate content between `limitations.md` and `reference/worker-loop.md`
- [ ] Old `docs/failure-modes.md` can be deleted

---

## 6. `docs/_research/doc-gap-analysis.md` Disposition

**Recommendation**: Move to `docs/implementation-specs/doc-gap-analysis.md` (same directory as this spec) as an **internal reference attachment**.

**Rationale**:
- It contains the competitive analysis that informed this spec — useful context for future doc maintainers
- It should NOT be in the user-facing docs tree (`docs/_research/` is fine for now but `_research` is not a standard docs category)
- Moving it to `docs/implementation-specs/` puts it alongside the spec it informed, making the "why" accessible to anyone reading the "how"
- It is explicitly NOT user-facing documentation; it's an internal artifact

**Alternative**: Delete it entirely. The gap analysis conclusions are fully captured in this spec's build cards and the original research is available in git history.

**Maintainer decision needed**: Move or delete? Default recommendation: **move to `docs/implementation-specs/doc-gap-analysis.md`**.

---

## Appendix: Files to Delete After Restructure

Once all new files from batches 2-5 are written and links updated, these old files should be removed:

| Old file | Replaced by |
|----------|-------------|
| `docs/quickstart.md` | `tutorials/quickstart.md` + `how-to/configure-provider.md` |
| `docs/concepts.md` | `concepts/*.md` (8 pages) |
| `docs/noeta-agent.md` | `reference/noeta-agent.md` + `reference/http-api.md` |
| `docs/noeta-architecture-deep-dive.md` | `architecture/overview.md` + `concepts/*.md` + `reference/comparison.md` |
| `docs/failure-modes.md` | `operations/troubleshooting.md` + `operations/limitations.md` |
| `docs/daemon.md` | `reference/worker-loop.md` + `reference/noeta-agent.md` (chat server section) |

**Note**: If the maintainer prefers redirect stubs (a short page that says "This page moved to X" with a link), keep the old files as stubs instead of deleting. This is a maintainer call.
