"""Running the channels a host enables (``noeta.core.channel``): each one's
``run`` in its own task until the host stops; one that fails is logged and
started again after a pause, so a dropped connection never ends the host."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Sequence

from noeta.core.channel import Channel
from noeta.core.client import ClientAPI

__all__ = ["run_channels"]

log = logging.getLogger("noeta.channels")


async def _keep_running(channel: Channel, client: ClientAPI, stop: asyncio.Event,
                        restart_after: float) -> None:
    while not stop.is_set():
        try:
            await channel.run(client, stop)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("channel %s failed; starting it again in %ss", channel.name,
                          restart_after)
        else:
            if stop.is_set():
                return
            log.warning("channel %s returned before the host stopped; starting it again",
                        channel.name)
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(stop.wait(), restart_after)


async def run_channels(channels: Sequence[Channel], client: ClientAPI, stop: asyncio.Event,
                       *, restart_after: float = 5.0) -> None:
    """Run ``channels`` until ``stop`` is set."""
    await asyncio.gather(*(_keep_running(c, client, stop, restart_after) for c in channels))
