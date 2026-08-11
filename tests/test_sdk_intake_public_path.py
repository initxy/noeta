"""Host-injected turn context reaches the ledger through the PUBLIC surface.

Two channels, deliberately different, and both reachable without touching a
private attribute:

* ``attachment_texts`` on the goal verbs — text the host already has at send
  time, recorded as its own ``origin="system"`` message BEFORE the goal.
* a ``reminder_provider`` plugin contribution — text that must be computed at
  recording time (it reads live state), recorded AFTER the goal.

The regression these pin is a surface gap, not a behaviour: ``attachment_texts``
existed on the driver but ``Client`` did not forward it, and ``Reminder`` /
``RecallView`` / ``TURN_INTAKE`` were not on ``noeta.sdk`` — so a host that
wanted either channel had to reach into ``Client._host``. What keeps that honest
here is the import list: this module and its plugin fixture name nothing outside
``noeta.sdk``, so the tests stop compiling the day the path needs a private one.
"""

from __future__ import annotations

from pathlib import Path

from tests._fixtures import host_intake_plugin as fixture

from noeta.sdk import (
    Client,
    InjectedMessage,
    LLMResponse,
    Options,
    TextBlock,
    Usage,
    UserMessage,
    load_plugins,
)
from noeta.sdk.testing import FakeLLMProvider


def _provider() -> FakeLLMProvider:
    return FakeLLMProvider(
        responses=[
            LLMResponse(
                stop_reason="end_turn",
                content=[TextBlock(text="ok")],
                usage=Usage(uncached=1, output=1),
            )
        ]
        * 4
    )


def _client(workspace: Path, **kwargs) -> Client:
    return Client(
        Options(
            system_prompt="you answer briefly",
            name="main",
            permission_mode="bypassPermissions",
            **kwargs.pop("options", {}),
        ),
        provider=_provider(),
        workspace_dir=workspace,
        model="stub-model",
        multi_turn=True,
        **kwargs,
    )


def _recorded(client: Client, task_id: str) -> list[tuple[str, str]]:
    """``(origin, text)`` for every user-channel item, in projection order.

    The human's own words project as ``UserMessage`` (origin ``"user"`` here);
    anything the host put on the same channel projects as ``InjectedMessage``
    carrying the seam that authored it.
    """
    out: list[tuple[str, str]] = []
    for item in client.messages(task_id):
        if isinstance(item, UserMessage):
            out.append(("user", item.text))
        elif isinstance(item, InjectedMessage):
            out.append((str(item.origin), item.text))
    return out


def test_attachment_texts_ride_the_goal_verbs(tmp_path: Path) -> None:
    client = _client(tmp_path)
    try:
        started = client.start(
            goal="the human's words",
            attachment_texts=("<attached>reference material</attached>",),
        )
        recorded = _recorded(client, started.task_id)
    finally:
        client.shutdown()

    texts = [t for _o, t in recorded]
    assert "<attached>reference material</attached>" in texts
    assert "the human's words" in texts
    # Attachments precede the goal, and carry the host-injected origin so the
    # transcript never attributes them to the person.
    assert texts.index("<attached>reference material</attached>") < texts.index(
        "the human's words"
    )
    assert dict(
        (t, o) for o, t in recorded
    )["<attached>reference material</attached>"] == "system"


def test_attachment_texts_default_is_inert(tmp_path: Path) -> None:
    """The added parameter must not move a byte for callers that ignore it."""
    client = _client(tmp_path)
    try:
        started = client.start(goal="the human's words")
        recorded = _recorded(client, started.task_id)
    finally:
        client.shutdown()
    assert [t for _o, t in recorded] == ["the human's words"]


# --- the provider channel ----------------------------------------------------


def test_reminder_provider_records_after_the_goal(tmp_path: Path) -> None:
    """The per-run text a host used to inject by mutating ``Client._host``,
    delivered instead by a plugin loaded through ``load_plugins``."""
    fixture.reset()
    fixture.bind("main", lambda: "workspace is quiet")
    client = _client(
        tmp_path,
        plugins=load_plugins(builtins=True, modules=(fixture.__name__,)),
        options={"plugins": ("host-intake",)},
    )
    try:
        started = client.start(goal="the human's words")
        recorded = _recorded(client, started.task_id)
    finally:
        client.shutdown()
        fixture.reset()

    texts = [t for _o, t in recorded]
    assert "workspace is quiet" in texts
    # The provider's output lands AFTER the goal — the half of the contract
    # ``attachment_texts`` cannot express.
    assert texts.index("the human's words") < texts.index("workspace is quiet")
    assert dict((t, o) for o, t in recorded)["workspace is quiet"] == "system"


def test_provider_reads_a_live_view_at_recording_time(tmp_path: Path) -> None:
    """Why this channel exists at all: the text is computed while the turn is
    being recorded, and the provider sees the task it is being recorded for."""
    fixture.reset()
    fixture.bind("main", lambda: "computed at recording time")
    client = _client(
        tmp_path,
        plugins=load_plugins(builtins=True, modules=(fixture.__name__,)),
        options={"plugins": ("host-intake",)},
    )
    try:
        started = client.start(goal="the human's words")
        seen = list(fixture.SEEN)
    finally:
        client.shutdown()
        fixture.reset()

    assert seen and seen[0].task_id == started.task_id
    assert "the human's words" in " ".join(
        getattr(b, "text", "") for b in seen[0].message
    )


def test_empty_provider_output_records_nothing(tmp_path: Path) -> None:
    """A provider with nothing to say must leave the turn byte-identical to a
    session that never loaded it."""
    fixture.reset()
    fixture.bind("main", lambda: "   ")
    client = _client(
        tmp_path,
        plugins=load_plugins(builtins=True, modules=(fixture.__name__,)),
        options={"plugins": ("host-intake",)},
    )
    try:
        started = client.start(goal="the human's words")
        recorded = _recorded(client, started.task_id)
    finally:
        client.shutdown()
        fixture.reset()
    assert [t for _o, t in recorded] == ["the human's words"]


def test_public_surface_carries_the_provider_vocabulary() -> None:
    """The gap that forced the private-attribute workaround: a host could name
    the contribution but not build its return value."""
    import noeta.sdk as sdk

    for name in ("Reminder", "RecallView", "ReminderProvider", "TURN_INTAKE"):
        assert name in sdk.__all__
        assert hasattr(sdk, name)
