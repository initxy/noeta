# Internal demos (contributor-facing)

These scripts are not SDK usage examples. They walk through Noeta's *internal*
mechanics — the lease-per-segment loop, the EventLog handshake — against a real
provider, and they sit apart from the SDK examples in the parent directory so a
library user reading `examples/` is not led into kernel internals.

Their job is to make current kernel behaviour observable: before changing a
mechanism, run the matching demo and watch what the kernel does end to end
instead of inferring it from the code.

| File | What it walks through | Needs a real LLM? |
| --- | --- | --- |
| [`real_provider_subtask_demo.py`](./real_provider_subtask_demo.py) | Subtask suspend / wake-resume across two Engines. | Yes — skips when the env is unset. |

Running one costs provider credit, so CI never does. Instead
[`tests/test_examples_demo.py`](../../tests/test_examples_demo.py) replays the
same flow against a scripted provider and checks that the script skips cleanly
when nothing is configured. A full agent run against a real model belongs to the
`live`-marked suite in
[`tests/test_live_context_supply_e2e.py`](../../tests/test_live_context_supply_e2e.py),
which takes the same environment variables as the demo above.
