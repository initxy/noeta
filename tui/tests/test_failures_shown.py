"""What the person asks for that fails is said in the transcript, and the
app goes on (a raise out of a key's handler ended it): a ``!`` command
printing bytes that are not UTF-8, a send to a server that is down, an
answer the server refuses, a ``/rewind`` the server refuses. ``/resume``
lists the root tasks however many sub-agents ran since."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import httpx

from noeta.host.app import LoggedWarnings
from noeta.testing import ScriptedProvider, reply
from noeta_tui.widgets import Choices, Notice

from support import AskBash, Lines, make_app, ready, settle, shown, type_line


def _refused(status: int, error: str) -> httpx.HTTPStatusError:
    """What ``RemoteClient`` raises for a request the server refuses."""
    request = httpx.Request("POST", "http://server/tasks")
    response = httpx.Response(status, request=request, json={"error": error})
    return httpx.HTTPStatusError(f"{status}", request=request, response=response)


async def test_a_bang_command_printing_bytes_not_utf8(tmp_path: Path) -> None:
    app = make_app(tmp_path, ScriptedProvider([]))
    async with app.run_test(size=(100, 30)) as pilot:
        await settle(pilot, lambda: ready(app))
        await type_line(pilot, "!printf 'caf\\351 ok'")
        await settle(pilot, lambda: "caf� ok" in shown(app))
    assert app.return_code is None


async def test_a_send_to_a_server_that_is_down(tmp_path: Path) -> None:
    app = make_app(tmp_path, ScriptedProvider([]))
    async with app.run_test(size=(100, 30)) as pilot:
        await settle(pilot, lambda: ready(app))

        async def down(*_: Any, **__: Any) -> int:
            raise httpx.ConnectError("All connection attempts failed")

        app.local.client.send = down
        await type_line(pilot, "hello")
        await settle(pilot, lambda: "sending it failed: All connection attempts failed"
                     in shown(app) and not app.busy)
    assert app.return_code is None


async def test_an_answer_the_server_refuses(tmp_path: Path) -> None:
    provider = ScriptedProvider([reply("", ("Bash", {"n": 1})), reply("ok")])
    app = make_app(tmp_path, provider, AskBash(), tools=[Lines()])
    async with app.run_test(size=(100, 30)) as pilot:
        await type_line(pilot, "run it")
        await settle(pilot, lambda: isinstance(app.focused, Choices))

        async def refused(*_: Any, **__: Any) -> int:
            raise _refused(409, "the task is not waiting on that request")

        app.local.client.answer = refused
        await pilot.press("y")
        await settle(pilot, lambda: "answering failed: the task is not waiting on that "
                     "request" in shown(app))
    assert app.return_code is None


async def test_a_rewind_the_server_refuses(tmp_path: Path) -> None:
    app = make_app(tmp_path, ScriptedProvider([reply("hi")]))
    async with app.run_test(size=(100, 30)) as pilot:
        await type_line(pilot, "hello")
        await settle(pilot, lambda: not app.busy and "hi" in shown(app))
        first = app.task_id

        async def refused(*_: Any, **__: Any) -> str:
            raise _refused(403, "a request may not set the task config key 'prompts'")

        app.local.client.create = refused
        await type_line(pilot, "/rewind 1 conversation")
        await settle(pilot, lambda: "rewound to before prompt 1" in shown(app))
        # The rewound conversation's task is made with its first message:
        # that is what the server refuses, and the rewind stays to go on with.
        await type_line(pilot, "hello again")
        await settle(pilot, lambda: "sending it failed: a request may not set the task "
                     "config key 'prompts'" in shown(app))
        assert app.task_id == "" and app.fork_from is not None
        assert app.fork_from[0] == first
    assert app.return_code is None



async def test_what_is_logged_is_said_in_the_transcript(tmp_path: Path) -> None:
    """stderr is under the screen: a warning logged before the app opens and
    an error logged while it runs are shown, each once."""
    held = LoggedWarnings()
    log = logging.getLogger("noeta.plugins.example")
    log.addHandler(held)
    try:
        log.warning("settings: a.json could not be read; ignored")
        app = make_app(tmp_path, ScriptedProvider([]), warnings=held)
        async with app.run_test(size=(100, 30)) as pilot:
            await settle(pilot, lambda: ready(app)
                         and "settings: a.json could not be read" in shown(app))
            log.error("mcp: the server x did not start")
            log.error("mcp: the server x did not start")
            await settle(pilot, lambda: "the server x did not start" in shown(app))
            assert [n for n in app.query(Notice) if "-error" in n.classes] != []
            assert shown(app).count("the server x did not start") == 1
    finally:
        log.removeHandler(held)
