"""The determinism guard must not reject a script for a parameter name.

Function/lambda parameters are ``ast.arg`` nodes, not ``ast.Name(Store)``. The
allowlist of locally-bound names must include them, or a purely deterministic
script like ``def f(io): ...`` is wrongly rejected by ``check_workflow_script``
as referencing a non-deterministic module — while an actual module load
(``result = time``) must still be rejected.
"""

from __future__ import annotations

import pytest

from noeta.builtins.react.impl.workflow_sandbox import check_workflow_script


@pytest.mark.parametrize(
    "script",
    [
        "def f(io):\n    return io + 1\nresult = f(2)\n",
        "g = lambda time: time * 2\nresult = g(3)\n",
        "def h(os, sys):\n    return os + sys\nresult = h(1, 2)\n",
        "def k(*uuid, **socket):\n    return len(uuid) + len(socket)\nresult = k(1, 2)\n",
    ],
)
def test_param_named_like_forbidden_module_is_allowed(script: str) -> None:
    assert check_workflow_script(script) is None


def test_actual_module_load_still_rejected() -> None:
    err = check_workflow_script("result = time\n")
    assert err is not None
    assert "time" in err
