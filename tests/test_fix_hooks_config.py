"""Regression: hooks-config notify.log must be type-validated (fail-closed).

A non-bool `log` (e.g. the string "false" or a typo "flase") used to be
silently coerced with bool(...), which is fail-open: bool("false") is True,
so logging stays on while the operator believes it is off. The strict
validator must instead raise HooksConfigError.
"""

import pytest

from noeta.agent.observe.hooks_config import HooksConfigError, parse_hooks_obj


def _obj(log_value):
    return {
        "post_tool_use": [
            {"match_tool": "shell_run", "notify": {"log": log_value}}
        ]
    }


@pytest.mark.parametrize("bad", ["false", "flase", "true", 1, 0, None, []])
def test_notify_log_non_bool_fails_closed(bad):
    with pytest.raises(HooksConfigError, match="notify.log must be a boolean"):
        parse_hooks_obj(_obj(bad))


def test_notify_log_true_is_accepted():
    cfg = parse_hooks_obj(_obj(True))
    assert cfg.post_tool_use[0].log is True
