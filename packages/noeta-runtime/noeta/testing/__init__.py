"""Test-only helpers for downstream Noeta users.

This package is the home of test doubles (``FakeLLMProvider``, future
``FakeContentStore`` / ``FakeDispatcher``). Production layers
(``noeta.core / noeta.runtime / noeta.policies / noeta.tools / noeta.context /
noeta.storage / noeta.providers``) must **not** import from
here; import-linter enforces the rule. Tests, examples, and user code
under ``tests/`` may import freely.
"""

from __future__ import annotations

__all__: list[str] = []
