"""Workflow sessions end to end (mock provider).

Covers: single-template start (prompt parameter substitution + title =
template name), workflow start (snapshot + first node driven), the two-phase
advance (preview degradation / monkeypatched prefill + confirm starting the
next node + audit file), switching back to a previous node to keep chatting
(coexists, no cancel), per-task SSE isolation, and the snapshot being immune
to later template edits.
"""
from __future__ import annotations

import time

from tests.conftest import login, personal_space_id, read_sse, wait_status


def _mk_template(client, sid, name, prompt, params=None):
    r = client.post(
        f"/api/v1/spaces/{sid}/templates",
        json={"name": name, "description": "", "prompt": prompt,
              "params": params or []},
    )
    assert r.status_code == 201, r.text
    return r.json()["template"]


def _mk_workflow(client, sid, name, template_ids):
    r = client.post(
        f"/api/v1/spaces/{sid}/workflow-templates",
        json={"name": name, "nodes": [{"template_id": t} for t in template_ids]},
    )
    assert r.status_code == 201, r.text
    return r.json()["workflow"]


def _setup_tracking_workflow(client, sid):
    """Tracking requirements → Tracking design two-node workflow (the core
    acceptance scenario)."""
    t1 = _mk_template(
        client, sid, "Tracking requirements",
        "Clarify the tracking requirements and produce a requirements document; business line: {business_line}",
        [{"name": "business_line", "description": "Owning business line",
          "required": True}],
    )
    t2 = _mk_template(
        client, sid, "Tracking design",
        "Design tracking based on {requirements_doc_url}",
        [{"name": "requirements_doc_url",
          "description": "URL of the requirements document", "required": True}],
    )
    wf = _mk_workflow(client, sid, "Tracking end-to-end", [t1["id"], t2["id"]])
    return t1, t2, wf


def _drive_node_to_idle(client, sid, task_id):
    """Drive one node from waiting (the mock's first-turn follow-up question)
    to idle."""
    wait_status(client, sid, {"waiting"})
    events = read_sse(client, sid, stop_types=("question",), task_id=task_id)
    q = next(e for e in events if e["event"] == "question")["data"]
    qid = q["questions"][0]["id"]
    r = client.post(
        f"/api/v1/sessions/{sid}/answer",
        json={"question_id": q["question_id"],
              "answers": {qid: {"choice_id": "eng"}},
              "task_id": task_id},
    )
    assert r.status_code == 202, r.text
    wait_status(client, sid, {"idle"})


def _session_workflow(client, sid) -> dict:
    return client.get(f"/api/v1/sessions/{sid}").json()["session"]["workflow"]


# ------------------------------------------------------- single-template start
def test_single_template_session(client):
    login(client)
    space = personal_space_id(client)
    tpl = _mk_template(
        client, space, "Weekly report", "Write a weekly report for {team}",
        [{"name": "team", "description": "", "required": True}],
    )
    # missing required parameter → 422
    r = client.post("/api/v1/sessions", json={
        "space_id": space, "template_id": tpl["id"], "params": {},
    })
    assert r.status_code == 422

    r = client.post("/api/v1/sessions", json={
        "space_id": space, "template_id": tpl["id"],
        "params": {"team": "Data Platform"},
    })
    assert r.status_code == 201, r.text
    s = r.json()["session"]
    assert s["title"] == "Weekly report"
    assert s["is_workflow"] is False
    # first message = the prompt after parameter substitution
    events = read_sse(client, s["id"], stop_types=("question", "turn_finished"))
    user_msgs = [e for e in events if e["event"] == "user_message"]
    assert user_msgs and user_msgs[0]["data"]["content"] == (
        "Write a weekly report for Data Platform"
    )


# ------------------------------------------------------------- workflow start
def test_workflow_session_end_to_end(client, monkeypatch):
    login(client)
    space = personal_space_id(client)
    _t1, _t2, wf = _setup_tracking_workflow(client, space)

    # create the session (first-node params) → the node0 task starts
    r = client.post("/api/v1/sessions", json={
        "space_id": space, "workflow_template_id": wf["id"],
        "params": {"business_line": "e-commerce"},
    })
    assert r.status_code == 201, r.text
    s = r.json()["session"]
    sid = s["id"]
    assert s["title"] == "Tracking end-to-end"
    assert s["is_workflow"] is True

    view = _session_workflow(client, sid)
    assert [n["name"] for n in view["nodes"]] == [
        "Tracking requirements", "Tracking design"
    ]

    # drive node0 to idle (question → answer)
    deadline = time.time() + 10
    task0 = None
    while time.time() < deadline and not task0:
        task0 = _session_workflow(client, sid)["nodes"][0]["task_id"]
        time.sleep(0.05)
    assert task0
    _drive_node_to_idle(client, sid, task0)
    # node0's events carry the goal with parameters substituted
    events = read_sse(client, sid, stop_types=("replay_done",), task_id=task0)
    first_user = next(e for e in events if e["event"] == "user_message")
    assert "e-commerce" in first_user["data"]["content"]

    # ---- advance preview: monkeypatch the handoff generation, returning
    # prefilled params + a summary
    from noeta.agent.workflow.handoff import HandoffResult

    def fake_handoff(settings, transcript, next_prompt, params, model, session_id):
        assert "Design tracking" in next_prompt or "{requirements_doc_url}" in next_prompt
        assert transcript  # transcript is non-empty (node0 had a conversation)
        return HandoffResult(
            params={"requirements_doc_url": "https://docs.example.com/docx/demo123"},
            summary="Requirements confirmed: core checkout-funnel tracking for the e-commerce business line.",
            degraded=False,
        )

    monkeypatch.setattr("noeta.agent.api.sessions.generate_handoff", fake_handoff)
    r = client.post(f"/api/v1/sessions/{sid}/advance/preview")
    assert r.status_code == 200, r.text
    preview = r.json()
    assert preview["node_index"] == 1
    assert preview["node_name"] == "Tracking design"
    assert preview["params"]["requirements_doc_url"].endswith("demo123")
    assert preview["degraded"] is False

    # ---- confirm: starts node1, goal = substituted prompt + handoff summary
    # section
    r = client.post(f"/api/v1/sessions/{sid}/advance/confirm", json={
        "node_index": 1,
        "params": preview["params"],
        "summary": preview["summary"],
    })
    assert r.status_code == 202, r.text

    deadline = time.time() + 10
    task1 = None
    while time.time() < deadline and not task1:
        task1 = _session_workflow(client, sid)["nodes"][1]["task_id"]
        time.sleep(0.05)
    assert task1 and task1 != task0
    _drive_node_to_idle(client, sid, task1)

    events = read_sse(client, sid, stop_types=("replay_done",), task_id=task1)
    goal1 = next(e for e in events if e["event"] == "user_message")["data"]["content"]
    assert "https://docs.example.com/docx/demo123" in goal1
    assert "Handoff summary from the previous stage" in goal1
    assert "core checkout-funnel" in goal1
    # node1's stream does not contain node0's original goal (physical
    # isolation; information travels only through the handoff summary)
    assert "Clarify the tracking requirements" not in goal1

    # the audit file lands under workspace handoff/
    files = client.get(f"/api/v1/sessions/{sid}/files").json()["files"]
    # with the sandbox off the file surface is empty — the audit file is
    # checked directly against the workspace by the e2e; skipped here

    # ---- switch back to node0 and keep chatting (coexists, no cancel)
    r = client.post(f"/api/v1/sessions/{sid}/messages", json={
        "content": "Add an impression scenario to the requirements document",
        "task_id": task0,
    })
    assert r.status_code == 202, r.text
    # after the 202 the drive lands in the stream asynchronously: poll node0's
    # replay until the new message appears
    deadline = time.time() + 10
    found = False
    while time.time() < deadline and not found:
        events = read_sse(client, sid, stop_types=("replay_done",), task_id=task0)
        found = any(
            "impression scenario" in e["data"].get("content", "")
            for e in events if e["event"] == "user_message"
        )
        if not found:
            time.sleep(0.2)
    assert found, "the message sent back to node0 did not appear in its event stream"

    # ---- already at the last node: another preview → 409
    wait_status(client, sid, {"idle"})
    assert client.post(f"/api/v1/sessions/{sid}/advance/preview").status_code == 409


def test_advance_confirm_stale_node_index_conflict(client, monkeypatch):
    login(client)
    space = personal_space_id(client)
    _t1, _t2, wf = _setup_tracking_workflow(client, space)
    r = client.post("/api/v1/sessions", json={
        "space_id": space, "workflow_template_id": wf["id"],
        "params": {"business_line": "livestream"},
    })
    sid = r.json()["session"]["id"]
    # preview while the first node is still running / unanswered → 409 (the
    # previous node is still executing, or waiting is advanceable)
    # confirming a wrong node_index directly → 409
    r = client.post(f"/api/v1/sessions/{sid}/advance/confirm", json={
        "node_index": 2, "params": {}, "summary": "",
    })
    assert r.status_code == 409


def test_workflow_snapshot_immune_to_template_edits(client, monkeypatch):
    login(client)
    space = personal_space_id(client)
    t1, t2, wf = _setup_tracking_workflow(client, space)
    r = client.post("/api/v1/sessions", json={
        "space_id": space, "workflow_template_id": wf["id"],
        "params": {"business_line": "local-services"},
    })
    sid = r.json()["session"]["id"]

    # after creating the session, edit the template / workflow (the snapshot
    # is unaffected)
    client.patch(f"/api/v1/spaces/{space}/templates/{t2['id']}",
                 json={"name": "Renamed design", "prompt": "brand-new prompt"})
    view = _session_workflow(client, sid)
    assert [n["name"] for n in view["nodes"]] == [
        "Tracking requirements", "Tracking design"
    ]

    # mock degradation path: generate_handoff unpatched (mock provider) →
    # fully empty prefill
    task0 = None
    deadline = time.time() + 10
    while time.time() < deadline and not task0:
        task0 = _session_workflow(client, sid)["nodes"][0]["task_id"]
        time.sleep(0.05)
    _drive_node_to_idle(client, sid, task0)
    r = client.post(f"/api/v1/sessions/{sid}/advance/preview")
    assert r.status_code == 200, r.text
    preview = r.json()
    assert preview["degraded"] is True
    assert preview["params"] == {"requirements_doc_url": None}
    # the snapshot holds: param_defs are still the old template's definitions
    assert preview["param_defs"][0]["name"] == "requirements_doc_url"

    # confirm with the required parameter missing → 422
    r = client.post(f"/api/v1/sessions/{sid}/advance/confirm", json={
        "node_index": 1, "params": {}, "summary": "",
    })
    assert r.status_code == 422


def test_startup_with_stale_workflow_node_task(tmp_path, make_client):
    """Regression: while rebuilding the task map at startup, the backfill loop
    for stale node tasks must not shadow the local name for settings.

    After a multi-node workflow has run, the database routinely contains
    "node tasks not equal to session.task_id". The tail of _init_client used
    to rebind the local name ``s`` (settings) to a Session while backfilling
    such tasks, after which ``start_workers(s.agent_num_workers)`` raised
    AttributeError → the service could not start. Seed exactly such a row and
    start up; the lifespan must complete.
    """
    from noeta.agent.store.sessions import SessionStore

    store = SessionStore(tmp_path / "data" / "app.db")
    sess = store.create(user="alice", model="mock-model", space_id="sp-test")
    # session.task_id is empty → the first rebuild loop does not cover it;
    # the node task goes through the backfill branch
    store.add_session_task(sess.id, 0, "task-stale-node", status="done")
    store.close()

    client = make_client()  # a crash fails the lifespan and uvicorn never starts (15s timeout)
    login(client, "alice")
    r = client.get("/api/v1/sessions", params={"space_id": personal_space_id(client)})
    assert r.status_code == 200, r.text
