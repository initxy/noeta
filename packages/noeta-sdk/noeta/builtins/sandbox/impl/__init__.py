"""The two AIO container adapters: an ``ExecEnv`` for files and processes, and
a ``BrowserBackend`` for the container's browser.

Each implements a Protocol owned elsewhere (``noeta.runtime.exec_env`` and the
``browser`` plugin respectively), and the SDK's ``SandboxExecEnvManager``
reaches them through the loader's dynamic-import doorway as its default
factories.
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
