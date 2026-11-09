"""A front end's screen covers stderr: what is logged while it runs is held,
then shown in it, each line once (``LoggedWarnings``)."""

from __future__ import annotations

import logging

from noeta.host.app import LoggedWarnings


def test_warnings_are_held_then_shown_once_each() -> None:
    held = LoggedWarnings()
    log = logging.getLogger("noeta.plugins.example")
    log.addHandler(held)
    try:
        log.warning("settings: %s could not be read", "a.json")
        log.info("not a warning")
        logging.getLogger("noeta.core.runner.task").addHandler(held)
        logging.getLogger("noeta.core.runner.task").error("task t: ended failed")
        log.warning("a notice the front end shows itself")
        held.forget(["a notice the front end shows itself"])
        shown: list[tuple[str, int]] = []
        held.show_with(lambda line, level: shown.append((line, level)))
        assert shown == [("settings: a.json could not be read", logging.WARNING)]
        try:
            raise OSError("no such file")
        except OSError:
            log.error("mcp: cannot start", exc_info=True)
        log.warning("settings: %s could not be read", "a.json")  # said already
        log.warning("a notice the front end shows itself")
        assert shown[1:] == [("mcp: cannot start: no such file", logging.ERROR)]
    finally:
        log.removeHandler(held)
        logging.getLogger("noeta.core.runner.task").removeHandler(held)
