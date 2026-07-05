"""``POST /tasks/{id}/events`` — the external-event wake endpoint.

The HTTP half of the exposed ``wait_external`` wake path (SDK half:
``tests/test_client_deliver_event.py``): the endpoint acks ``202`` like its
sibling command verbs, the delivered ``payload`` rides the resumed turn as a
recorded message, and a task not waiting on the given ``event_kind`` maps to
``409`` via the typed ``not_resumable`` code.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from noeta.agent.backend import BackendConfig, EngineRoom, serve_backend
from noeta.agent.spec import ComponentRef
from noeta.protocols.decisions import FinishDecision, WaitExternalDecision
from noeta.sdk import Options
from noeta.testing.fake_llm import FakeLLMProvider


EVENT_KIND = "webhook:payment"


class _WaitExternalThenFinishPolicy:
    """Scripted: suspend on ``wait_external`` once, then finish."""

    def __init__(self) -> None:
        self._decisions = [
            WaitExternalDecision(event_kind=EVENT_KIND),
            FinishDecision(answer="paid"),
        ]

    def decide(self, ctx, view):  # noqa: ARG002 — scripted
        return self._decisions.pop(0)


class _WaitExternalPolicyProvider:
    """``Options.policy`` shape: ``(llm) -> Policy`` carrying a ``.ref``."""

    @property
    def ref(self) -> ComponentRef:
        return ComponentRef("wait-external-scripted", "1")

    def __call__(self, llm) -> _WaitExternalThenFinishPolicy:  # noqa: ARG002
        return _WaitExternalThenFinishPolicy()


def _room(workspace: Path) -> EngineRoom:
    return EngineRoom(
        Options(
            system_prompt="you wait for an external event",
            name="main",
            allowed_tools=(),
            policy=_WaitExternalPolicyProvider(),
            permission_mode="bypassPermissions",
        ),
        provider=FakeLLMProvider(responses=[]),  # scripted policy: never called
        workspace_dir=workspace,
    )


def _post(url: str, body: dict[str, Any]) -> Any:
    req = urllib.request.Request(
        url,
        data=json.dumps(body).encode("utf-8"),
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    return urllib.request.urlopen(req, timeout=5)


def test_deliver_event_endpoint_acks_202_and_wakes_the_task(
    tmp_path: Path,
) -> None:
    room = _room(tmp_path)
    config = BackendConfig(host="127.0.0.1", port=0, workspace_dir=tmp_path)
    server, url, shutdown = serve_backend(config, engine_room=room)
    try:
        task_id = room.start(goal="wait for the payment webhook")
        types = [e.type for e in room.events(task_id)]
        assert "TaskSuspended" in types  # waiting_external

        with _post(
            url + f"tasks/{task_id}/events",
            {"event_kind": EVENT_KIND, "payload": {"amount": 42}},
        ) as resp:
            assert resp.status == 202
            assert json.loads(resp.read()) == {"task_id": task_id}

        types = [e.type for e in room.events(task_id)]
        assert "TaskWoken" in types
        # The resumed turn ran to the trailing next-goal suspend (multi-turn
        # room: the scripted FinishDecision translates to the next-goal
        # yield), with the payload notice recorded on the message view.
        assert types[-1] == "TaskSuspended"
        assert types.index("TaskWoken") < len(types) - 1
        texts = [
            str(getattr(m, "text", getattr(m, "answer", "")))
            for m in room.messages(task_id)
        ]
        assert any("<external-event kind=\"webhook:payment\">" in t for t in texts)

        # Idempotency mirrors a repeat answer: the wake was consumed, so a
        # repeat delivery is a typed 409, never a silent 202.
        raised: urllib.error.HTTPError | None = None
        try:
            _post(
                url + f"tasks/{task_id}/events", {"event_kind": EVENT_KIND}
            )
        except urllib.error.HTTPError as exc:
            raised = exc
        assert raised is not None and raised.code == 409
        assert json.loads(raised.read())["code"] == "not_resumable"
    finally:
        shutdown()
        room.shutdown()


def test_deliver_event_wrong_kind_maps_to_409_not_resumable(
    tmp_path: Path,
) -> None:
    room = _room(tmp_path)
    config = BackendConfig(host="127.0.0.1", port=0, workspace_dir=tmp_path)
    server, url, shutdown = serve_backend(config, engine_room=room)
    try:
        task_id = room.start(goal="wait for the payment webhook")
        raised: urllib.error.HTTPError | None = None
        try:
            _post(
                url + f"tasks/{task_id}/events",
                {"event_kind": "webhook:other"},
            )
        except urllib.error.HTTPError as exc:
            raised = exc
        assert raised is not None, "expected an HTTP error status, not success"
        assert raised.code == 409
        assert json.loads(raised.read())["code"] == "not_resumable"
        # The mis-delivery consumed nothing: the correct kind still wakes it.
        with _post(
            url + f"tasks/{task_id}/events", {"event_kind": EVENT_KIND}
        ) as resp:
            assert resp.status == 202
        assert "TaskWoken" in [e.type for e in room.events(task_id)]
    finally:
        shutdown()
        room.shutdown()
