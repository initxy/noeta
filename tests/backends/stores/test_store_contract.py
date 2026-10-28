"""The store contract (``noeta.testing.store_contract``) for each store this
package ships."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from noeta.backends.stores.sqlite import SqliteStore
from noeta.core.store import Store
from noeta.testing.store_contract import *  # noqa: F403


@pytest.fixture(params=["sqlite", "postgres"])
async def store(request: pytest.FixtureRequest, tmp_path: Path) -> AsyncIterator[Store]:
    s: Store
    if request.param == "postgres":
        s = request.getfixturevalue("pg").store()
    else:
        s = SqliteStore(str(tmp_path / "t.db"))
    yield s
    await s.close()
