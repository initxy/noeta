"""Space agent-config API: member read, owner write, validation, and the
partial-update / clear-selection semantics of the store behind it."""
from __future__ import annotations

from tests.conftest import login, personal_space_id


def _new_team(client, name="Team A") -> str:
    resp = client.post("/api/v1/spaces", json={"name": name, "description": ""})
    assert resp.status_code == 201, resp.text
    return resp.json()["space"]["id"]


def test_defaults_when_never_configured(client):
    login(client, "alice")
    sid = personal_space_id(client)
    config = client.get(f"/api/v1/spaces/{sid}/agent-config").json()["config"]
    assert config == {
        "prompt": "",
        "memory_enabled": True,
        "knowledge_sources": None,
        "default_model": "",
        "default_effort": "",
    }


def test_owner_updates_member_reads_nonmember_404(client):
    login(client, "alice")
    sid = _new_team(client)
    client.post(f"/api/v1/spaces/{sid}/members", json={"username": "bob"})

    r = client.put(
        f"/api/v1/spaces/{sid}/agent-config",
        json={"prompt": "  Be terse.  ", "default_effort": "high"},
    )
    assert r.status_code == 200
    config = r.json()["config"]
    assert config["prompt"] == "Be terse."  # stripped
    assert config["default_effort"] == "high"
    assert config["memory_enabled"] is True  # untouched fields keep defaults

    # A member reads the same config but may not write it.
    login(client, "bob")
    got = client.get(f"/api/v1/spaces/{sid}/agent-config")
    assert got.status_code == 200
    assert got.json()["config"]["prompt"] == "Be terse."
    denied = client.put(
        f"/api/v1/spaces/{sid}/agent-config", json={"prompt": "mine now"}
    )
    assert denied.status_code == 403

    # A non-member sees neither (existence hidden).
    login(client, "carol")
    assert client.get(f"/api/v1/spaces/{sid}/agent-config").status_code == 404


def test_knowledge_selection_set_and_clear(client):
    login(client, "alice")
    sid = personal_space_id(client)
    put = lambda body: client.put(  # noqa: E731 - local shorthand
        f"/api/v1/spaces/{sid}/agent-config", json=body
    ).json()["config"]

    assert put({"knowledge_sources": ["src1", "src2"]})["knowledge_sources"] == [
        "src1",
        "src2",
    ]
    # Partial update elsewhere keeps the selection.
    assert put({"prompt": "x"})["knowledge_sources"] == ["src1", "src2"]
    # clear_knowledge_sources resets to None (= all sources) and wins over a
    # simultaneously-sent list.
    assert (
        put({"clear_knowledge_sources": True, "knowledge_sources": ["src3"]})[
            "knowledge_sources"
        ]
        is None
    )


def test_model_and_effort_validation(client):
    login(client, "alice")
    sid = personal_space_id(client)
    url = f"/api/v1/spaces/{sid}/agent-config"

    assert client.put(url, json={"default_model": "no-such-model"}).status_code == 422
    assert client.put(url, json={"default_effort": "extreme"}).status_code == 422

    # The configured models.json models validate; empty string resets both.
    models = client.get("/api/v1/models").json()["models"]
    ok = client.put(url, json={"default_model": models[0]["id"]})
    assert ok.status_code == 200
    assert ok.json()["config"]["default_model"] == models[0]["id"]
    reset = client.put(url, json={"default_model": "", "default_effort": ""})
    assert reset.json()["config"]["default_model"] == ""
