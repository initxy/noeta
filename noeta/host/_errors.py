"""What a client is told about an error a request ran into."""

from __future__ import annotations

import logging

log = logging.getLogger("noeta.host")

#: Said instead of the text of an error noeta did not raise itself.
UNEXPECTED = "the request failed on the server; its log has the details"


def error_text(exc: BaseException) -> str:
    """``exc``'s text when noeta's own code raised it (written for the
    caller), else :data:`UNEXPECTED`, with the error logged: a library's
    or the system's text may name the server's paths."""
    tb = exc.__traceback__
    while tb is not None and tb.tb_next is not None:
        tb = tb.tb_next
    module = tb.tb_frame.f_globals.get("__name__", "") if tb is not None else ""
    if module == "noeta" or str(module).startswith("noeta."):
        return str(exc) or type(exc).__name__
    log.error("request failed", exc_info=exc)
    return UNEXPECTED
