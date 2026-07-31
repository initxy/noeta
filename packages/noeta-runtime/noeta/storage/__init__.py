"""L2 storage — the InMemory reference backend plus the public backend SPI.

:mod:`noeta.storage.memory` is the executable definition of the L0 storage
Protocols' semantics; :mod:`noeta.storage.spi` is what a backend author builds
against, fronting the domain rules every backend must share. The durable
backends live in the ``storage`` built-in (``noeta.builtins.storage.impl``,
noeta-sdk), reached through :mod:`noeta.sdk.storage`."""

from __future__ import annotations

__all__: list[str] = []
