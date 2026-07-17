"""Task board (space collaboration layer Phase 2): store / API / tool tests."""
from __future__ import annotations

import httpx
import pytest

from noeta.agent.store.board import BoardStore

from tests.conftest import login, personal_space_id


def team_space(client: httpx.Client, name: str = "Board team") -> str:
    resp = client.post("/api/v1/spaces", json={"name": name})
    assert resp.status_code == 201, resp.text
    return resp.json()["space"]["id"]


# ------------------------------------------------------------------ store
def test_board_store(tmp_path):
    store = BoardStore(tmp_path / "t.db")
    a = store.create_card("sp", "Task A", "alice")
    b = store.create_card("sp", "Task B", "alice", column_key="doing")
    c = store.create_card("sp", "Task C", "bob")
    # position increases within the same column
    assert c["position"] > a["position"]
    assert [x["title"] for x in store.list_cards("sp")] == ["Task B", "Task A", "Task C"]

    store.update_card(a["id"], title="Renamed", assignee="bob", due_date="2026-08-01")
    got = store.get_card(a["id"])
    assert got["title"] == "Renamed" and got["assignee"] == "bob"
    with pytest.raises(ValueError):
        store.update_card(a["id"], column_key="nope")
    with pytest.raises(ValueError):
        store.update_card(a["id"], bad_field=1)

    # moving to another column lands at the target column's end
    moved = store.move_to_column_end(a["id"], "doing")
    assert moved["column_key"] == "doing" and moved["position"] > b["position"]

    # back-link append is idempotent
    link = {"type": "topic", "id": "t1", "channel_id": "c1", "label": "Topic"}
    store.add_link(a["id"], link)
    store.add_link(a["id"], link)
    assert store.get_card(a["id"])["links"] == [link]

    store.delete_card(a["id"])
    assert store.get_card(a["id"]) is None


# -------------------------------------------------------------------- API
def test_board_api_crud_permissions(client):
    login(client, "alice")
    personal = personal_space_id(client)
    resp = client.post(
        f"/api/v1/spaces/{personal}/board/cards", json={"title": "should not exist"}
    )
    assert resp.status_code == 422

    space_id = team_space(client)
    resp = client.post(
        f"/api/v1/spaces/{space_id}/board/cards",
        json={"title": "Pick up the tracking request", "description": "d",
              "due_date": "2026-08-01"},
    )
    assert resp.status_code == 201, resp.text
    card = resp.json()["card"]

    # non-member: 404 / 403
    login(client, "mallory")
    assert client.get(f"/api/v1/spaces/{space_id}/board").status_code == 403
    assert (
        client.patch(
            f"/api/v1/board/cards/{card['id']}", json={"title": "x"}
        ).status_code
        == 404
    )

    # a member can create/move cards, but cannot delete someone else's card
    login(client, "bob")
    login(client, "alice")
    client.post(f"/api/v1/spaces/{space_id}/members", json={"username": "bob"})
    login(client, "bob")
    resp = client.patch(
        f"/api/v1/board/cards/{card['id']}",
        json={"column_key": "doing", "position": 5.0},
    )
    assert resp.status_code == 200
    assert resp.json()["card"]["column_key"] == "doing"
    assert (
        client.delete(f"/api/v1/board/cards/{card['id']}").status_code == 403
    )
    # the creator can delete their own card
    mine = client.post(
        f"/api/v1/spaces/{space_id}/board/cards", json={"title": "bob's card"}
    ).json()["card"]
    assert client.delete(f"/api/v1/board/cards/{mine['id']}").status_code == 200
    # the owner can delete any card
    login(client, "alice")
    assert client.delete(f"/api/v1/board/cards/{card['id']}").status_code == 200


def test_board_card_start_session_and_topic_to_card(client):
    login(client, "alice")
    space_id = team_space(client)

    # create template → start a session from a card → link written back +
    # appears in my sessions
    resp = client.post(
        f"/api/v1/spaces/{space_id}/templates",
        json={
            "name": "Triage template",
            "description": "",
            "prompt": "Triage: {issue}",
            "params": [{"name": "issue", "required": True}],
        },
    )
    assert resp.status_code == 201, resp.text
    tpl = resp.json()["template"]
    card = client.post(
        f"/api/v1/spaces/{space_id}/board/cards", json={"title": "Triage data loss"}
    ).json()["card"]

    # missing required parameter → 422
    resp = client.post(
        f"/api/v1/board/cards/{card['id']}/start-session",
        json={"template_id": tpl["id"], "params": {}},
    )
    assert resp.status_code == 422
    resp = client.post(
        f"/api/v1/board/cards/{card['id']}/start-session",
        json={"template_id": tpl["id"], "params": {"issue": "order events dropped"}},
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    session_id = body["session"]["id"]
    assert body["card"]["links"] == [
        {"type": "session", "id": session_id, "label": "Triage data loss"}
    ]
    sessions = client.get(f"/api/v1/sessions?space_id={space_id}").json()["sessions"]
    assert [s["id"] for s in sessions] == [session_id]

    # one-click channel-topic-to-card: title prefilled with the root message +
    # topic back-link
    channel = client.post(
        f"/api/v1/spaces/{space_id}/channels", json={"name": "Review"}
    ).json()["channel"]
    resp = client.post(
        f"/api/v1/channels/{channel['id']}/messages",
        json={"text": "@Agent draft a tracking plan", "mention_agent": True},
    )
    topic = resp.json()["topic"]
    resp = client.post(
        f"/api/v1/channels/{channel['id']}/topics/{topic['id']}/to-card"
    )
    assert resp.status_code == 201, resp.text
    tcard = resp.json()["card"]
    assert tcard["title"] == "@Agent draft a tracking plan"
    assert tcard["links"][0]["type"] == "topic"
    assert tcard["links"][0]["channel_id"] == channel["id"]


# ------------------------------------------------------------- agent tools
def test_board_tools_invoke(tmp_path):
    from noeta.agent.host.board_tools import build_board_tools

    store = BoardStore(tmp_path / "t.db")

    def resolve_space(task_id):
        return "sp" if task_id == "task1" else None

    def resolve_topic_link(task_id):
        return (
            {"type": "topic", "id": "t1", "channel_id": "c1", "label": "Topic"}
            if task_id == "task1"
            else None
        )

    class Ctx:
        def __init__(self, task_id):
            self.metadata = {"task_id": task_id}
            self.artifact_store = None

    blist, bcreate, bupdate, bmove = build_board_tools(
        lambda: store, resolve_space, resolve_topic_link
    )

    # personal space / unknown task: all rejected
    assert not blist.invoke({}, Ctx("other")).success

    out = bcreate.invoke({"title": "Note the metric-definition task"}, Ctx("task1"))
    assert out.success, out.output
    card = store.list_cards("sp")[0]
    # a card created inside a channel topic automatically gets the back-link
    assert card["links"][0]["id"] == "t1"

    out = blist.invoke({}, Ctx("task1"))
    assert out.success and "Note the metric-definition task" in out.output and card["id"] in out.output

    out = bupdate.invoke(
        {"card_id": card["id"], "assignee": "bob", "due_date": "2026-08-01"},
        Ctx("task1"),
    )
    assert out.success
    assert store.get_card(card["id"])["assignee"] == "bob"

    out = bmove.invoke({"card_id": card["id"], "column": "done"}, Ctx("task1"))
    assert out.success
    assert store.get_card(card["id"])["column_key"] == "done"

    # nonexistent card / invalid column
    assert not bmove.invoke({"card_id": "nope", "column": "done"}, Ctx("task1")).success
    assert not bmove.invoke({"card_id": card["id"], "column": "bad"}, Ctx("task1")).success
