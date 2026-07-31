"""L2 storage — the InMemory reference backend plus the public backend SPI.

:mod:`noeta.storage.memory` is the kernel's reference backend (the
executable definition of the L0 storage Protocols' semantics);
:mod:`noeta.storage.spi` is the public SPI a backend author builds
against, fronting the private shared domain rules
(``_payload_restore`` / ``_reclaim`` / ``_wake_match``). The durable
backends (sqlite, Postgres) live in the ``storage`` built-in
(``noeta.builtins.storage.impl``, noeta-sdk); their doorway is
:mod:`noeta.sdk.storage`."""

from __future__ import annotations

__all__: list[str] = []
