"""Files the person mentions with ``@path``, attached inside the task through
``Read`` (Claude Code's at-mention): the read counts for a later ``Edit``,
the read limits hold, a deny rule keeps a file out, and a task with no exec
env (or attached over HTTP) still gets told what was mentioned."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from noeta.backends.execenvs.local import LocalExecEnv
from noeta.backends.stores.sqlite import SqliteStore
from noeta.core.agent import Agent
from noeta.core.client import Client
from noeta.core.events import ToolFinished
from noeta.core.extension import Ask, Deny, Extension, TaskView
from noeta.core.runner import Runtime
from noeta.core.types import Image, Message, Text, ToolUse
from noeta.plugins.fs import FileTools
from noeta.plugins.fs.mentions import Mention, mentioned
from noeta.testing import ScriptedProvider, reply
from tests.images import png


async def _no_sleep(_: float) -> None:
    return None


class Guard(Extension):
    """Denies reading anything named ``secret``; asks about everything else."""

    name = "guard"

    async def before_tool(self, view: TaskView, call: ToolUse) -> Any:
        if "secret" in str(call.input.get("file_path", "")):
            return Deny(reason="no")
        view.record(self.name, "asked", {"id": call.id})
        return Ask(prompt="ok?")


async def run(tmp_path: Path, prompt: str, *replies: Any, env: bool = True,
              extensions: list[Extension] | None = None) -> tuple[ScriptedProvider, Client, str]:
    work = tmp_path / "w"
    store = SqliteStore(str(tmp_path / "t.db"))
    provider = ScriptedProvider(list(replies) or [reply("ok")])
    agent = Agent(name="a", model="m", extensions=[FileTools(), *(extensions or [])])
    rt = Runtime(store=store, agents={"a": agent}, provider_for=lambda _m: provider,
                 env_for=lambda _s: LocalExecEnv(str(work)) if env else None, sleep=_no_sleep)
    client = Client(store)
    tid = await client.create("a", prompt)
    await rt.run_until_idle()
    return provider, client, tid


def attached(provider: ScriptedProvider, n: int = 0) -> list[Message]:
    return [m for m in provider.requests[n].messages if m.role == "user" and m.origin == "system"
            and ("Called the" in m.text or "@-mentioned" in m.text)]


@pytest.fixture
def work(tmp_path: Path) -> Path:
    path = tmp_path / "w"
    path.mkdir()
    return path


def test_mention_forms() -> None:
    assert mentioned('see @"a b.txt", @c.txt. mail me@c.txt @src/x.py#L3-9 @y#L4 '
                     "@file:///abs/z.md @c.txt") == [
        Mention("a b.txt"), Mention("c.txt"), Mention("src/x.py", 3, 9), Mention("y", 4),
        Mention("/abs/z.md")]


async def test_a_mentioned_file_is_read_and_can_be_edited_at_once(tmp_path: Path,
                                                                  work: Path) -> None:
    calc = work / "calc.py"
    calc.write_text("def div(a, b):\n    return a * b\n")
    provider, client, tid = await run(
        tmp_path, "fix div in @calc.py",
        reply("", ("Edit", {"file_path": str(calc), "old_string": "a * b",
                            "new_string": "a / b"})),
        reply("fixed"))
    [note] = attached(provider)
    call, result = note.content
    assert call.text == ('Called the Read tool with the following input: '  # type: ignore[union-attr]
                         + json.dumps({"file_path": str(calc)}, separators=(",", ":")))
    assert result.text == ("Result of calling the Read tool:\n"  # type: ignore[union-attr]
                           "1\tdef div(a, b):\n2\t    return a * b\n3\t")
    # The person's text stays as typed; the attachment follows it.
    prompt = provider.requests[0].messages.index(note) - 1
    assert provider.requests[0].messages[prompt].text == "fix div in @calc.py"
    results = [r.event.result for r in await client.events(tid)
               if isinstance(r.event, ToolFinished)]
    assert [r.is_error for r in results] == [False], results[0].text
    assert calc.read_text() == "def div(a, b):\n    return a / b\n"
    # Attached once: the second request carries the same single note.
    assert len(attached(provider, 1)) == 1


async def test_lines_a_directory_an_image_and_what_cannot_be_read(tmp_path: Path,
                                                                 work: Path) -> None:
    (work / "notes.txt").write_text("".join(f"line {n}\n" for n in range(1, 10)))
    (work / "pkg").mkdir()
    (work / "pkg" / "a.py").write_text("")
    (work / "pkg" / "sub").mkdir()
    (work / "shot.png").write_bytes(png(3, 3))
    (work / "data.zip").write_bytes(b"PK\x03\x04")
    provider, _, _ = await run(
        tmp_path, "@notes.txt#L2-3 and @pkg and @shot.png and @data.zip and @nowhere.txt")
    lines, listing, image, unread = attached(provider)
    assert '"offset":2,"limit":2' in lines.content[0].text  # type: ignore[union-attr]
    assert lines.content[1].text.endswith("2\tline 2\n3\tline 3")  # type: ignore[union-attr]
    assert listing.content[0].text.startswith(  # type: ignore[union-attr]
        'Called the Bash tool with the following input: {"command":"ls ')
    assert listing.content[1].text == "Result of calling the Bash tool:\na.py\nsub/"  # type: ignore[union-attr]
    assert isinstance(image.content[1], Image)
    assert unread.text.startswith(f"The user @-mentioned {work / 'data.zip'}. They could not be")
    assert "nowhere" not in "".join(m.text for m in attached(provider))


async def test_a_long_file_is_cut_to_its_first_lines_and_a_huge_one_is_named(
        tmp_path: Path, work: Path) -> None:
    (work / "long.txt").write_text("".join(f"{n:06d} " + "x" * 33 + "\n" for n in range(4000)))
    (work / "huge.txt").write_text("".join("y" * 199 + "\n" for _ in range(3000)))
    provider, _, _ = await run(tmp_path, "read @long.txt and @huge.txt")
    long, huge = attached(provider)
    assert '"limit":2000' in long.content[0].text  # type: ignore[union-attr]
    assert "\n2000\t001999 " in long.content[1].text  # type: ignore[union-attr]
    assert "2001\t" not in long.content[1].text  # type: ignore[union-attr]
    assert "only its first 2000 lines are shown" in long.content[2].text  # type: ignore[union-attr]
    assert huge.text.startswith(f"The user @-mentioned {work / 'huge.txt'} (") and \
        "too large to read at once" in huge.text


async def test_a_deny_rule_keeps_a_file_out_and_an_ask_does_not(tmp_path: Path,
                                                                work: Path) -> None:
    (work / "secret.env").write_text("TOKEN=1\n")
    (work / "open.txt").write_text("hello\n")
    provider, client, tid = await run(tmp_path, "@secret.env @open.txt", extensions=[Guard()])
    [note] = attached(provider)
    assert "open.txt" in note.text and "TOKEN" not in note.text
    # The checks were only asked: nothing they recorded stands.
    assert not any(getattr(r.event, "extension", "") == "guard" for r in await client.events(tid))


async def test_without_an_exec_env_the_mention_is_named(tmp_path: Path, work: Path) -> None:
    provider, _, _ = await run(tmp_path, "look at @src/app.py, thanks @bob", env=False)
    [note] = attached(provider)
    assert note.text == ("The user @-mentioned src/app.py. Files are not attached automatically "
                         "here: if these are files or directories in your working directory, "
                         "read them with your file tools before you answer.")


async def test_only_the_persons_new_messages_are_expanded(tmp_path: Path, work: Path) -> None:
    (work / "a.txt").write_text("a\n")
    store = SqliteStore(str(tmp_path / "t.db"))
    provider = ScriptedProvider([reply("one"), reply("two")])
    rt = Runtime(store=store, agents={"a": Agent(name="a", model="m", extensions=[FileTools()])},
                 provider_for=lambda _m: provider, env_for=lambda _s: LocalExecEnv(str(work)),
                 sleep=_no_sleep)
    client = Client(store)
    tid = await client.create("a", "see @a.txt")
    await rt.run_until_idle()
    await client.send(tid, "and again, no mention")
    await rt.run_until_idle()
    assert len(attached(provider, 1)) == 1
    # A prompt not from the person (a sub-agent's, from its parent) is not expanded.
    provider.add(reply("three"))
    await client.create("a", Message(role="user", content=[Text(text="see @a.txt")]))
    await rt.run_until_idle()
    assert attached(provider, 2) == []


async def test_mentions_are_attached_on_the_server(tmp_path: Path, work: Path) -> None:
    pytest.importorskip("starlette")
    uvicorn = pytest.importorskip("uvicorn")
    from noeta.host.http import create_app
    from noeta.host.local import LocalApp
    from noeta.host.remote import RemoteClient

    (work / "calc.py").write_text("x = 1\n")
    provider = ScriptedProvider([reply("seen")])
    app = LocalApp(SqliteStore(str(tmp_path / "t.db")),
                   [Agent(name="a", model="m", extensions=[FileTools()])], provider,
                   env_for=lambda _s: LocalExecEnv(str(work)))
    await app.start()
    server = uvicorn.Server(uvicorn.Config(create_app(app.client, token="t"),
                                           host="127.0.0.1", port=0, log_level="error"))
    serving = asyncio.create_task(server.serve())
    while not server.started:
        await asyncio.sleep(0.01)
    client = RemoteClient(f"http://127.0.0.1:{server.servers[0].sockets[0].getsockname()[1]}",
                          token="t")
    try:
        tid = await client.create("a", "what is in @calc.py")
        await client.wait(tid, timeout=20)
        [note] = attached(provider)
        assert note.content[1].text == "Result of calling the Read tool:\n1\tx = 1\n2\t"  # type: ignore[union-attr]
    finally:
        await client.close()
        server.should_exit = True
        await serving
        await app.stop()


async def test_a_long_pdf_is_named_with_how_to_read_it(tmp_path: Path, work: Path,
                                                       monkeypatch: pytest.MonkeyPatch) -> None:
    import noeta.plugins.fs.mentions as mentions

    async def pages(*_a: Any) -> int:
        return 30

    monkeypatch.setattr(mentions, "page_count", pages)
    (work / "spec.pdf").write_bytes(b"%PDF-1.4\n" + b"0" * 2000)
    provider, _, _ = await run(tmp_path, "summarise @spec.pdf")
    [note] = [m for m in provider.requests[0].messages if m.text.startswith("PDF file:")]
    assert note.origin == "system" and "(30 pages, " in note.text and 'pages: "1-5"' in note.text
