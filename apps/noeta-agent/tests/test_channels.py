"""Channels (space collaboration layer): store / API / topic-flow tests.

The store uses real sqlite (tmp_path); the API flow runs on LiveServer + the
mock provider — an @Agent mention starts a real root task (the mock's first
turn is ask_user_question → waiting), which exercises the topic status
projection and answering the follow-up question.
"""
from __future__ import annotations

import json
import time
from typing import Optional

import httpx
import pytest

from noeta.agent.store.channels import ChannelStore

from tests.conftest import login, personal_space_id


# ------------------------------------------------------------------ store
def test_channel_store_crud(tmp_path):
    store = ChannelStore(tmp_path / "t.db")
    c = store.create_channel("sp_a", "Tracking plan review", "alice", "discussion channel")
    assert c["name"] == "Tracking plan review" and not c["archived"]
    assert store.get_channel(c["id"])["description"] == "discussion channel"
    assert [x["id"] for x in store.list_channels("sp_a")] == [c["id"]]
    store.update_channel(c["id"], name="New name", archived=1)
    assert store.list_channels("sp_a") == []
    assert store.list_channels("sp_a", include_archived=True)[0]["name"] == "New name"
    store.update_channel(c["id"], session_id="sess1", archived=0)
    assert store.get_channel_by_session("sess1")["id"] == c["id"]
    with pytest.raises(ValueError):
        store.update_channel(c["id"], nope=1)
    store.delete_channel(c["id"])
    assert store.get_channel(c["id"]) is None


def test_channel_messages_paging(tmp_path):
    store = ChannelStore(tmp_path / "t.db")
    c = store.create_channel("sp", "ch", "alice")
    seqs = [store.add_message(c["id"], "alice", f"m{i}")["seq"] for i in range(5)]
    assert store.get_message(seqs[0])["text"] == "m0"
    # the latest page (ascending)
    page = store.list_messages(c["id"], limit=3)
    assert [m["text"] for m in page] == ["m2", "m3", "m4"]
    # page backwards via before_seq
    page = store.list_messages(c["id"], before_seq=page[0]["seq"], limit=10)
    assert [m["text"] for m in page] == ["m0", "m1"]
    # replay via after_seq
    page = store.list_messages(c["id"], after_seq=seqs[1], limit=10)
    assert [m["text"] for m in page] == ["m2", "m3", "m4"]
    assert store.latest_seq(c["id"]) == seqs[-1]


def test_channel_topics_and_reads(tmp_path):
    store = ChannelStore(tmp_path / "t.db")
    c = store.create_channel("sp", "ch", "alice")
    store.update_channel(c["id"], session_id="sess1")
    m = store.add_message(c["id"], "alice", "@Agent draft a plan")
    t = store.add_topic(c["id"], m["seq"], 0, "alice")
    store.set_message_topic(m["seq"], t["id"])
    assert store.get_message(m["seq"])["topic_id"] == t["id"]
    assert store.get_topic(t["id"])["node_index"] == 0
    assert store.get_topic_by_node(c["id"], 0)["id"] == t["id"]
    assert [x["id"] for x in store.list_topics(c["id"])] == [t["id"]]
    store.update_topic_preview(t["id"], "The conclusion is…")
    assert store.get_topic(t["id"])["last_reply_preview"] == "The conclusion is…"
    assert store.channels_with_topics() == ["sess1"]

    # unread: bob has 1 unread (sent by alice); alice's own messages don't count
    assert store.unread_counts("sp", "bob") == {c["id"]: 1}
    assert store.unread_counts("sp", "alice") == {}
    store.set_read(c["id"], "bob", m["seq"])
    assert store.unread_counts("sp", "bob") == {}
    # the watermark only moves forward, never backward
    store.set_read(c["id"], "bob", 0)
    assert store.unread_counts("sp", "bob") == {}


# -------------------------------------------------------------------- API
def team_space(client: httpx.Client, name: str = "Collab team") -> str:
    resp = client.post("/api/v1/spaces", json={"name": name})
    assert resp.status_code == 201, resp.text
    return resp.json()["space"]["id"]


def wait_topic(
    client: httpx.Client,
    channel_id: str,
    want: set[str],
    timeout: float = 20.0,
) -> dict:
    """Poll the channel topic snapshot until the first topic reaches a target
    status."""
    deadline = time.time() + timeout
    last: Optional[dict] = None
    while time.time() < deadline:
        topics = client.get(
            f"/api/v1/channels/{channel_id}/messages"
        ).json()["topics"]
        if topics:
            last = topics[0]
            if last["status"] in want and last["task_id"]:
                return last
        time.sleep(0.1)
    raise AssertionError(f"topic status stuck at {last}, wanted {want}")


def test_channel_crud_permissions(client):
    login(client, "alice")
    # personal spaces do not support channels
    personal = personal_space_id(client)
    resp = client.post(
        f"/api/v1/spaces/{personal}/channels", json={"name": "should not exist"}
    )
    assert resp.status_code == 422

    space_id = team_space(client)
    resp = client.post(
        f"/api/v1/spaces/{space_id}/channels",
        json={"name": "Review", "description": "d"},
    )
    assert resp.status_code == 201
    channel = resp.json()["channel"]
    assert channel["unread"] == 0

    # non-member: list 403, channel operations 404 (hide existence)
    login(client, "mallory")
    assert client.get(f"/api/v1/spaces/{space_id}/channels").status_code == 403
    assert (
        client.get(f"/api/v1/channels/{channel['id']}/messages").status_code == 404
    )
    assert (
        client.post(
            f"/api/v1/channels/{channel['id']}/messages",
            json={"text": "hi"},
        ).status_code
        == 404
    )

    # a member (non-owner) can post, but cannot modify the channel
    login(client, "bob")
    login(client, "alice")
    resp = client.post(
        f"/api/v1/spaces/{space_id}/members", json={"username": "bob"}
    )
    assert resp.status_code == 201, resp.text
    login(client, "bob")
    assert (
        client.post(
            f"/api/v1/channels/{channel['id']}/messages",
            json={"text": "bob's message"},
        ).status_code
        == 202
    )
    assert (
        client.patch(
            f"/api/v1/channels/{channel['id']}", json={"name": "renamed"}
        ).status_code
        == 403
    )
    # the owner can rename/archive; posting to an archived channel is 409
    login(client, "alice")
    resp = client.patch(
        f"/api/v1/channels/{channel['id']}", json={"archived": True}
    )
    assert resp.status_code == 200 and resp.json()["channel"]["archived"]
    assert (
        client.post(
            f"/api/v1/channels/{channel['id']}/messages",
            json={"text": "x"},
        ).status_code
        == 409
    )


def test_channel_messages_unread_and_stream(client):
    login(client, "bob")  # register bob first
    login(client, "alice")
    space_id = team_space(client)
    client.post(f"/api/v1/spaces/{space_id}/members", json={"username": "bob"})
    channel = client.post(
        f"/api/v1/spaces/{space_id}/channels", json={"name": "Chit-chat"}
    ).json()["channel"]
    cid = channel["id"]

    r1 = client.post(
        f"/api/v1/channels/{cid}/messages", json={"text": "the first one"}
    ).json()["message"]
    client.post(f"/api/v1/channels/{cid}/messages", json={"text": "the second one"})

    # alice's own messages don't count as unread; bob has 2 unread
    unread = {
        c["id"]: c["unread"]
        for c in client.get(f"/api/v1/spaces/{space_id}/channels").json()["channels"]
    }
    assert unread[cid] == 0
    login(client, "bob")
    unread = {
        c["id"]: c["unread"]
        for c in client.get(f"/api/v1/spaces/{space_id}/channels").json()["channels"]
    }
    assert unread[cid] == 2
    # advancing the watermark clears the count
    latest = client.get(f"/api/v1/channels/{cid}/messages").json()["messages"][-1]
    client.put(f"/api/v1/channels/{cid}/read", json={"seq": latest["seq"]})
    unread = {
        c["id"]: c["unread"]
        for c in client.get(f"/api/v1/spaces/{space_id}/channels").json()["channels"]
    }
    assert unread[cid] == 0

    # SSE: since_seq replay excludes the first message, includes the second +
    # topics_snapshot + replay_done
    events = read_channel_sse(client, cid, since_seq=r1["seq"])
    types = [e["type"] for e in events]
    assert "replay_done" in types and "topics_snapshot" in types
    texts = [e["data"]["text"] for e in events if e["type"] == "message"]
    assert texts == ["the second one"]


def test_channel_topic_flow(client):
    login(client, "alice")
    space_id = team_space(client)
    channel = client.post(
        f"/api/v1/spaces/{space_id}/channels", json={"name": "Plans"}
    ).json()["channel"]
    cid = channel["id"]

    # human chat as groundwork (material for context injection)
    client.post(f"/api/v1/channels/{cid}/messages",
                json={"text": "let's discuss the metric definition first"})

    # @Agent starts a topic (mock first turn ask_user_question → waiting)
    resp = client.post(
        f"/api/v1/channels/{cid}/messages",
        json={"text": "@Agent draft a plan based on the discussion",
              "mention_agent": True},
    )
    assert resp.status_code == 202, resp.text
    body = resp.json()
    assert body["topic"] is not None
    assert body["message"]["topic_id"] == body["topic"]["id"]

    topic = wait_topic(client, cid, {"waiting", "idle"})
    tid = topic["id"]

    # the channel session is a sentinel session: it does not appear in the
    # "my sessions" list
    sessions = client.get(f"/api/v1/sessions?space_id={space_id}").json()["sessions"]
    assert sessions == []

    # the topic view rides the existing session SSE: space members can
    # subscribe (a 404 would mean the authorization surface broke)
    channel_detail = [
        c for c in client.get(f"/api/v1/spaces/{space_id}/channels").json()["channels"]
        if c["id"] == cid
    ][0]
    session_id = channel_detail["session_id"]
    assert session_id
    detail = client.get(f"/api/v1/sessions/{session_id}")
    assert detail.status_code == 200

    if topic["status"] == "waiting":
        # plain text inside the topic = answering the follow-up (no @ needed),
        # driving the mock into the next turn
        resp = client.post(
            f"/api/v1/channels/{cid}/topics/{tid}/messages",
            json={"text": "Use UV as the metric"},
        )
        assert resp.status_code == 202, resp.text
        topic = wait_topic(client, cid, {"idle"})

    # after the turn ends there is a reply preview (mock end_turn has an
    # assistant body)
    assert topic["status"] == "idle"

    # start another topic: node_index increments, context injection includes
    # historical topics (not crashing = the chain works)
    resp = client.post(
        f"/api/v1/channels/{cid}/messages",
        json={"text": "@Agent add a bit more", "mention_agent": True},
    )
    assert resp.status_code == 202
    topics = client.get(f"/api/v1/channels/{cid}/messages").json()["topics"]
    assert len(topics) == 2
    assert topics[1]["node_index"] == topics[0]["node_index"] + 1

    # nonexistent topic → 404
    assert (
        client.post(
            f"/api/v1/channels/{cid}/topics/nope/messages", json={"text": "x"}
        ).status_code
        == 404
    )


def read_channel_sse(
    client: httpx.Client,
    channel_id: str,
    since_seq: Optional[int] = None,
    timeout: float = 10.0,
) -> list[dict]:
    """Read the channel SSE up to replay_done (for verifying replay
    semantics)."""
    url = f"/api/v1/channels/{channel_id}/stream"
    if since_seq is not None:
        url += f"?since_seq={since_seq}"
    events: list[dict] = []
    with client.stream("GET", url, timeout=timeout) as resp:
        assert resp.status_code == 200
        etype = ""
        for line in resp.iter_lines():
            if line.startswith("event:"):
                etype = line.split(":", 1)[1].strip()
            elif line.startswith("data:"):
                events.append(
                    {"type": etype, "data": json.loads(line.split(":", 1)[1])}
                )
                if etype == "replay_done":
                    return events
    return events


# ------------------------------------------------------------- agent tools
def test_channel_tools_invoke(tmp_path):
    """channel_read_history / channel_read_topic invoked directly (fake ctx)."""
    from noeta.agent.host.channel_tools import build_channel_tools

    store = ChannelStore(tmp_path / "t.db")
    c = store.create_channel("sp", "ch", "alice")
    store.update_channel(c["id"], session_id="sess1")
    for i in range(3):
        store.add_message(c["id"], "alice", f"m{i}")
    channel = store.get_channel(c["id"])

    class FakeSvc:
        def resolve_channel_for_task(self, task_id):
            return channel if task_id == "task1" else None

        def read_history(self, ch, before_seq, limit):
            return store.list_messages(ch["id"], before_seq=before_seq, limit=limit)

        def read_topic(self, ch, topic_id):
            return "[user]\ndraft a plan\n\n[assistant]\nconclusion" if topic_id == "t1" else None

    class Ctx:
        def __init__(self, task_id):
            self.metadata = {"task_id": task_id}
            self.artifact_store = None

    history, topic = build_channel_tools(lambda: FakeSvc())

    out = history.invoke({}, Ctx("task1"))
    assert out.success and "[alice] m2" in out.output
    out = history.invoke({"limit": 1}, Ctx("task1"))
    assert out.output.count("[alice]") == 1
    # non-channel task: failure hint
    out = history.invoke({}, Ctx("other"))
    assert not out.success and "channel" in out.output

    out = topic.invoke({"topic_id": "t1"}, Ctx("task1"))
    assert out.success and "conclusion" in out.output
    out = topic.invoke({"topic_id": "nope"}, Ctx("task1"))
    assert not out.success


def test_channel_topic_memory_lands_in_space_dir(client, tmp_path):
    """e2e: the agent writes a memory inside a channel topic → the resolver
    persists it to this space's memory directory keyed by the channel
    session's space_id (the channel sentinel session does not break
    task→space resolution)."""
    login(client, "alice")
    space_id = team_space(client, "Memory channel team")
    channel = client.post(
        f"/api/v1/spaces/{space_id}/channels", json={"name": "Metrics"}
    ).json()["channel"]
    cid = channel["id"]

    resp = client.post(
        f"/api/v1/channels/{cid}/messages",
        json={"text": "@Agent please remember: I prefer concise replies",
              "mention_agent": True},
    )
    assert resp.status_code == 202, resp.text
    wait_topic(client, cid, {"idle", "waiting"})

    path = tmp_path / "data" / "memories" / space_id / "user-preference-demo.md"
    assert path.is_file(), (
        "channel-topic memory did not land in the space directory "
        "(resolver not effective?)"
    )

    r = client.get(f"/api/v1/spaces/{space_id}/memories")
    names = [m["name"] for m in r.json()["memories"]]
    assert "user-preference-demo" in names


def test_channel_stream_live_push(client):
    """SSE live frames: while the stream stays open, someone else's message →
    the hub pushes it in real time (not the replay path)."""
    import threading

    login(client, "alice")
    space_id = team_space(client, "Live team")
    channel = client.post(
        f"/api/v1/spaces/{space_id}/channels", json={"name": "live"}
    ).json()["channel"]
    cid = channel["id"]
    first = client.post(
        f"/api/v1/channels/{cid}/messages", json={"text": "one that exists first"}
    ).json()["message"]

    got: list[str] = []
    ready = threading.Event()

    def listen() -> None:
        with client.stream(
            "GET", f"/api/v1/channels/{cid}/stream?since_seq={first['seq']}",
            timeout=15,
        ) as resp:
            etype = ""
            for line in resp.iter_lines():
                if line.startswith("event:"):
                    etype = line.split(":", 1)[1].strip()
                elif line.startswith("data:"):
                    if etype == "replay_done":
                        ready.set()
                    elif etype == "message":
                        got.append(json.loads(line.split(":", 1)[1])["text"])
                        return

    t = threading.Thread(target=listen, daemon=True)
    t.start()
    assert ready.wait(10), "SSE replay did not finish"
    client.post(f"/api/v1/channels/{cid}/messages", json={"text": "a live one"})
    t.join(timeout=10)
    assert got == ["a live one"]
