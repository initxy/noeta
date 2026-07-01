# Internal demos (contributor-facing)

These scripts are **not** SDK usage examples — they are real-provider
acceptance gates that walk through Noeta's *internal* mechanics (the
lease-per-segment loop, EventLog recording). They live here, separate
from the SDK examples in the parent directory, so a library user reading
`examples/` is not led into kernel internals.

They are **kept, not deleted** on purpose. They are working samples of how
the kernel behaves — a Chesterton's-fence guard: before changing a kernel
mechanism, a contributor can run the matching demo to see the current
behaviour spelled out end-to-end, rather than guessing what some seam was
for.

| File | What it walks through | Needs a real LLM? |
| --- | --- | --- |
| [`real_provider_subtask_demo.py`](./real_provider_subtask_demo.py) | Real-provider sub-task suspend / wake-resume across two Engines. | Yes — skips when env is unset. |

A full bug-fixer coding agent against a real model now runs through the shipping
backend (`NOETA_AGENT_CONFIG=… python -m noeta.agent`) and the env-gated live suite
(`tests/test_live_context_supply_e2e.py`).

The real-provider demos are exercised by humans with their own API key;
their deterministic golden paths are also covered in
[`tests/test_examples_demo.py`](../../tests/test_examples_demo.py).
