"""``sandbox`` built-in — the AIO execution adapters (impl).

Microkernel M2: the two retirement-slated AIO adapters moved here —
:class:`~noeta.builtins.sandbox.impl.exec_env.AioSandboxExecEnv` out of
``noeta.runtime.exec_env`` (which keeps the ``ExecEnv`` Protocol +
``LocalExecEnv``) and
:class:`~noeta.builtins.sandbox.impl.browser.AioBrowserBackend` out of
``noeta.tools.browser._backend`` (which keeps the ``BrowserBackend``
Protocol). The parent manifest declares both on the host-plane
``sandbox_provider`` surface; the SDK's ``SandboxExecEnvManager`` resolves
them through the loader's dynamic-import doorway as its default factories.
"""

from __future__ import annotations

from noeta.builtins.sandbox.impl.browser import AioBrowserBackend, AioBrowserError
from noeta.builtins.sandbox.impl.exec_env import (
    DEFAULT_AIO_TIMEOUT_S,
    AioHttpPost,
    AioSandboxError,
    AioSandboxExecEnv,
)


__all__ = [
    "AioBrowserBackend",
    "AioBrowserError",
    "AioHttpPost",
    "AioSandboxError",
    "AioSandboxExecEnv",
    "DEFAULT_AIO_TIMEOUT_S",
]
