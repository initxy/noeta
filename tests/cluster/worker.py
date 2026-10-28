"""One cluster worker process: ``noeta worker`` with a short lease.

``python worker.py STORE_URL CWD LEASE_SECONDS``. Everything else comes the
way ``noeta worker`` reads it: ``NOETA_HOME`` (agent files, config),
``NOETA_MODEL`` / ``NOETA_PROVIDER`` / ``NOETA_MODELS_FILE``. The only
difference from ``noeta.cli.main._worker`` is ``lease_seconds``, which the
command has no flag for (its 30 s default would make every recovery in the
test take a minute).
"""

from __future__ import annotations

import asyncio
import logging
import signal
import sys

from noeta.cli.config import from_settings, load_settings


async def main(url: str, cwd: str, lease: float) -> None:
    settings = load_settings(cwd=cwd, store=url)
    app = from_settings(settings, cwd=cwd, lease_seconds=lease, concurrency=8)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)
    await app.runtime.serve(stop)
    await app.store.close()


if __name__ == "__main__":
    logging.basicConfig(level=logging.WARNING,
                        format="%(asctime)s %(process)d %(name)s %(levelname)s %(message)s")
    asyncio.run(main(sys.argv[1], sys.argv[2], float(sys.argv[3])))
