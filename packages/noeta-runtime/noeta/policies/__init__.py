"""The control MECHANISM band: routing types, dispatcher, shared neutral
helpers, and deterministic test Policies.

Policy implementations are plugin material — the kernel builder receives the
default through ``default_policy_factory`` — so this band carries zero product
schema, description, or translate body. Everything the built-ins import from it
is public-named: that import crosses the wheel boundary, and an underscore would
be a contract the kernel does not actually offer.
"""

from __future__ import annotations

__all__: list[str] = []
