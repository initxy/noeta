"""``python -m noeta.agent`` — the thin launcher for the official code agent.

D5/D6 (issue TL5). NOT an argparse CLI and takes **zero** positional
args: it reads config from env / an optional ``NOETA_AGENT_CONFIG`` file
(:meth:`noeta.agent.backend.lifecycle.BackendConfig.from_env`), boots the
noeta.sdk-eating backend (:func:`noeta.agent.backend.lifecycle.serve_backend`),
prints the served URL, then blocks until SIGINT / SIGTERM and shuts down
cleanly. Boots fully offline by default (the
:class:`noeta.agent.observe._stub_provider.CodeStubProvider`).
"""

from __future__ import annotations

import signal
import sys
import threading


def _serve() -> tuple[object, str, "object"]:
    """Boot the backend; return ``(server, url, shutdown)``.

    The noeta.sdk-eating backend
    is the only backend — the web frontend folds the task protocol (one
    multiplexed SSE stream + the command endpoints + /content). The legacy
    ``noeta.server`` runner was removed in T8 once runtime parity was verified.
    """
    from noeta.agent.backend.lifecycle import BackendConfig, serve_backend

    return serve_backend(BackendConfig.from_env())


def main() -> int:
    server, url, shutdown = _serve()
    print(f"noeta.agent serving at {url}", flush=True)
    print(f"chat composer at {url}chat", flush=True)

    stop = threading.Event()

    def _handle_signal(signum: int, frame: object) -> None:  # noqa: ARG001
        stop.set()

    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)

    try:
        stop.wait()
    finally:
        shutdown()
    return 0


if __name__ == "__main__":
    sys.exit(main())
