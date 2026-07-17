"""Feedback-loop API: collection / references / permissions / analysis-run
lifecycle / adoption.

The analysis e2e goes through the mock LLM's attribution branch (the
"negative feedback items" route in mock_llm.py): read the transcript of the
task that received feedback → submit_suggestion (evidence citing the
feedback_id) → wrap-up TaskSuspended → run done + feedback marked analyzed.
"""
from __future__ import annotations

import time

from tests.conftest import create_session, login, personal_space_id, wait_status


def _drive_turn(client, sid: str, content: str = "remember that I prefer concise replies") -> None:
    """Drive one turn to idle (the "remember" route does memory_write →
    end_turn, a single-turn wrap-up)."""
    resp = client.post(f"/api/v1/sessions/{sid}/messages", json={"content": content})
    assert resp.status_code == 202, resp.text
    wait_status(client, sid, {"idle"})


def _submit(client, sid: str, rating: int = -1, **overrides) -> dict:
    body = {
        "rating": rating,
        "event_seq": 3,
        "tags": ["incorrect result"],
        "comment": "the conclusion does not match reality",
        **overrides,
    }
    resp = client.post(f"/api/v1/sessions/{sid}/feedback", json=body)
    assert resp.status_code == 200, resp.text
    return resp.json()["feedback"]


def _new_team(client, name="Team F") -> str:
    r = client.post("/api/v1/spaces", json={"name": name, "description": ""})
    assert r.status_code == 201, r.text
    return r.json()["space"]["id"]


def test_feedback_submit_and_list(client):
    login(client)
    space_id = personal_space_id(client)
    sid = create_session(client)
    _drive_turn(client, sid)

    fb_neg = _submit(client, sid, rating=-1)
    assert fb_neg["space_id"] == space_id
    assert fb_neg["task_id"]  # without an explicit task_id, falls back to session.task_id
    assert fb_neg["tags"] == ["incorrect result"]
    assert fb_neg["reference_kind"] == "none"

    # 👍 only records the count; invalid tags are discarded
    fb_pos = _submit(client, sid, rating=1, tags=["nonexistent tag"], comment="")
    assert fb_pos["tags"] == []

    # Session-level list (the frontend marks messages that already have feedback)
    r = client.get(f"/api/v1/sessions/{sid}/feedback")
    assert r.status_code == 200 and len(r.json()["feedback"]) == 2

    # Space-level list + positive/negative counts
    r = client.get(f"/api/v1/spaces/{space_id}/feedback")
    assert r.status_code == 200
    data = r.json()
    assert data["counts"] == {"positive": 1, "negative": 1}
    assert len(data["feedback"]) == 2
    assert data["tags"]  # preset tags delivered for the frontend chips

    # Invalid rating
    r = client.post(
        f"/api/v1/sessions/{sid}/feedback", json={"rating": 0}
    )
    assert r.status_code == 422


def test_feedback_permissions(make_client):
    owner = make_client()
    login(owner, "alice")
    team_id = _new_team(owner)

    # bob joins as a member; charlie is not a member
    r = owner.post(f"/api/v1/spaces/{team_id}/members", json={"username": "bob"})
    assert r.status_code == 201, r.text

    member = make_client()
    login(member, "bob")
    outsider = make_client()
    login(outsider, "charlie")

    # Non-member: the feedback page is 404 (hiding existence)
    assert outsider.get(f"/api/v1/spaces/{team_id}/feedback").status_code == 404

    # Members can submit (team-space session) + view. No real turn is driven:
    # the multi-client fixtures share one noeta.db, and a WorkerLoop on
    # another server would steal the lease so this server never observes the
    # status events; feedback submission does not depend on a driven task
    # anyway (task_id may be empty).
    sid = create_session(member, team_id)
    fb = _submit(member, sid)
    assert member.get(f"/api/v1/spaces/{team_id}/feedback").status_code == 200

    # Members can attach a reference (text)
    r = member.put(
        f"/api/v1/spaces/{team_id}/feedback/{fb['id']}/reference",
        json={"kind": "text", "text": "the correct conclusion is plan B"},
    )
    assert r.status_code == 200, r.text

    # Triggering analysis / adopting / dismissing: members get 403
    assert (
        member.post(f"/api/v1/spaces/{team_id}/feedback/analyze").status_code == 403
    )
    assert (
        member.post(
            f"/api/v1/spaces/{team_id}/feedback/suggestions/nonexistent/adopt",
            json={},
        ).status_code
        == 403
    )
    assert (
        member.post(
            f"/api/v1/spaces/{team_id}/feedback/suggestions/nonexistent/dismiss"
        ).status_code
        == 403
    )


def test_reference_text_snapshot(client, tmp_path):
    login(client)
    space_id = personal_space_id(client)
    sid = create_session(client)
    _drive_turn(client, sid)
    fb = _submit(client, sid)

    # Empty text → 400
    r = client.put(
        f"/api/v1/spaces/{space_id}/feedback/{fb['id']}/reference",
        json={"kind": "text", "text": "   "},
    )
    assert r.status_code == 400

    r = client.put(
        f"/api/v1/spaces/{space_id}/feedback/{fb['id']}/reference",
        json={"kind": "text", "text": "# Final version\n\nThe correct tracking plan is X."},
    )
    assert r.status_code == 200, r.text
    assert r.json()["feedback"]["reference_kind"] == "text"

    # The snapshot is materialized on disk
    # (DATA_DIR/feedback/<space>/<fid>/reference.md)
    snapshot = tmp_path / "data" / "feedback" / space_id / fb["id"] / "reference.md"
    assert snapshot.is_file() and "correct tracking plan" in snapshot.read_text("utf-8")

    # GET the reference
    r = client.get(f"/api/v1/spaces/{space_id}/feedback/{fb['id']}/reference")
    assert r.status_code == 200 and "Final version" in r.json()["text"]

    # Feedback without a reference → 404
    fb2 = _submit(client, sid)
    r = client.get(f"/api/v1/spaces/{space_id}/feedback/{fb2['id']}/reference")
    assert r.status_code == 404

    # Non-text kind → 422 (the doc-kind flow is gone; text is the only kind)
    r = client.put(
        f"/api/v1/spaces/{space_id}/feedback/{fb2['id']}/reference",
        json={"kind": "doc", "text": "irrelevant"},
    )
    assert r.status_code == 422
    assert "kind must be text" in r.json()["detail"]


def _wait_run(client, space_id: str, want: set[str], timeout: float = 25.0) -> dict:
    deadline = time.time() + timeout
    run: dict = {}
    while time.time() < deadline:
        run = client.get(f"/api/v1/spaces/{space_id}/feedback/runs/latest").json()[
            "run"
        ] or {}
        if run.get("status") in want:
            return run
        time.sleep(0.1)
    raise AssertionError(f"run status stuck at {run.get('status')!r}, wanted {want}")


def test_analysis_run_and_adopt_e2e(client, tmp_path):
    login(client)
    space_id = personal_space_id(client)
    sid = create_session(client)
    _drive_turn(client, sid)
    fb = _submit(client, sid)

    # Before any negative feedback: positive feedback does not count as
    # pending analysis (fb is negative; first verify the full run chain, then
    # the incremental semantics)
    r = client.post(f"/api/v1/spaces/{space_id}/feedback/analyze")
    assert r.status_code == 200, r.text
    assert r.json()["feedback_count"] == 1

    # Triggering again while the run is in flight → 409 (serialized per
    # space); within seconds the mock attribution wraps up → done
    second = client.post(f"/api/v1/spaces/{space_id}/feedback/analyze")
    assert second.status_code in (409, 400)  # a fast machine may already be done → nothing pending (400)
    run = _wait_run(client, space_id, {"done", "failed"})
    assert run["status"] == "done", run

    # The suggestion is persisted: memory channel + evidence citing that
    # feedback_id
    r = client.get(f"/api/v1/spaces/{space_id}/feedback/suggestions")
    suggestions = r.json()["suggestions"]
    assert len(suggestions) == 1, suggestions
    sug = suggestions[0]
    assert sug["channel"] == "memory" and sug["status"] == "pending"
    assert sug["evidence"][0]["feedback_id"] == fb["id"]

    # The feedback is marked analyzed: triggering again finds nothing → 400
    r = client.post(f"/api/v1/spaces/{space_id}/feedback/analyze")
    assert r.status_code == 400

    # Attaching a reference → analyzed resets, analyzable again
    r = client.put(
        f"/api/v1/spaces/{space_id}/feedback/{fb['id']}/reference",
        json={"kind": "text", "text": "reference for the correct result"},
    )
    assert r.status_code == 200
    r = client.post(f"/api/v1/spaces/{space_id}/feedback/analyze")
    assert r.status_code == 200
    _wait_run(client, space_id, {"done"})

    # Adopt the memory suggestion (owner-edited draft) → space memory written
    # to disk
    r = client.post(
        f"/api/v1/spaces/{space_id}/feedback/suggestions/{sug['id']}/adopt",
        json={"memory_name": "feedback-conclusion-check", "memory_text": (
            "---\ndescription: behavior correction adopted from feedback\ntype: feedback\n---\n\n"
            "Verify the key data before stating conclusions."
        )},
    )
    assert r.status_code == 200, r.text
    assert r.json()["suggestion"]["status"] == "adopted"
    assert r.json()["suggestion"]["adopted_result"] == {
        "memory": "feedback-conclusion-check"
    }
    memory_file = (
        tmp_path / "data" / "memories" / space_id / "feedback-conclusion-check.md"
    )
    assert memory_file.is_file() and "Verify the key data" in memory_file.read_text("utf-8")

    # An already-handled suggestion cannot be decided again → 409
    r = client.post(
        f"/api/v1/spaces/{space_id}/feedback/suggestions/{sug['id']}/dismiss"
    )
    assert r.status_code == 409

    # Adopting a memory suggestion without a draft → 422 (a new suggestion
    # produced by the second run)
    r = client.get(f"/api/v1/spaces/{space_id}/feedback/suggestions")
    pending = [s for s in r.json()["suggestions"] if s["status"] == "pending"]
    assert pending, "the second run should produce another suggestion"
    r = client.post(
        f"/api/v1/spaces/{space_id}/feedback/suggestions/{pending[0]['id']}/adopt",
        json={},
    )
    assert r.status_code == 422

    # Dismiss
    r = client.post(
        f"/api/v1/spaces/{space_id}/feedback/suggestions/{pending[0]['id']}/dismiss"
    )
    assert r.status_code == 200
    assert r.json()["suggestion"]["status"] == "dismissed"


def test_skill_patch_adopt_e2e(make_client, tmp_path):
    """Phase 2: a skill-channel suggestion carries skill_patch → diff preview
    → adoption = backup + overwrite SKILL.md.

    The mock attribution branch submits a skill suggestion for the
    `skill:<name>` mentioned in the feedback note (a patch only applies to an
    existing space skill; the test drops one into the shared directory
    first).
    """
    client = make_client(SHARED_DATA_DIR=str(tmp_path / "shared"))
    login(client)
    space_id = personal_space_id(client)

    skill_dir = tmp_path / "shared" / "space-skills" / space_id / "demo-fb-skill"
    skill_dir.mkdir(parents=True)
    original = "---\nname: demo-fb-skill\n---\n\n# demo-fb-skill original\n"
    (skill_dir / "SKILL.md").write_text(original, encoding="utf-8")

    sid = create_session(client)
    _drive_turn(client, sid)
    _submit(client, sid, comment="the output is wrong, the rules of skill:demo-fb-skill are ambiguous")

    r = client.post(f"/api/v1/spaces/{space_id}/feedback/analyze")
    assert r.status_code == 200, r.text
    _wait_run(client, space_id, {"done"})

    r = client.get(f"/api/v1/spaces/{space_id}/feedback/suggestions")
    sug = r.json()["suggestions"][0]
    assert sug["channel"] == "skill" and sug["skill_name"] == "demo-fb-skill"
    assert sug["skill_patch"]

    # Diff preview (member-readable): current = the original text, patched =
    # the full patch text
    r = client.get(
        f"/api/v1/spaces/{space_id}/feedback/suggestions/{sug['id']}/skill-diff"
    )
    assert r.status_code == 200, r.text
    diff = r.json()
    assert diff["current"] == original and "mock patch" in diff["patched"]

    # Adoption = backup + apply
    r = client.post(
        f"/api/v1/spaces/{space_id}/feedback/suggestions/{sug['id']}/adopt",
        json={},
    )
    assert r.status_code == 200, r.text
    result = r.json()["suggestion"]["adopted_result"]
    assert result["skill"] == "demo-fb-skill" and result["backup"]
    applied = (skill_dir / "SKILL.md").read_text("utf-8")
    assert "mock patch" in applied
    backup_file = (
        tmp_path / "data" / "feedback" / space_id / "skill-backups" / result["backup"]
    )
    assert backup_file.is_file() and backup_file.read_text("utf-8") == original


def test_report_generate_and_publish(client, tmp_path):
    """Phase 2: select suggestions → a report-mode run produces a draft;
    publishing writes a markdown file under data/reports and records its path
    in doc_url (deliberate difference from the source: publishing no longer
    targets a hosted document, so the un-configured-authorization 400 gate is
    gone — publish succeeds offline)."""
    login(client)
    space_id = personal_space_id(client)
    sid = create_session(client)
    _drive_turn(client, sid)
    _submit(client, sid)

    r = client.post(f"/api/v1/spaces/{space_id}/feedback/analyze")
    assert r.status_code == 200
    _wait_run(client, space_id, {"done"})
    sug = client.get(f"/api/v1/spaces/{space_id}/feedback/suggestions").json()[
        "suggestions"
    ][0]

    # Generate the report (report-mode run)
    r = client.post(
        f"/api/v1/spaces/{space_id}/feedback/report",
        json={"suggestion_ids": [sug["id"]]},
    )
    assert r.status_code == 200, r.text
    assert r.json()["run"]["kind"] == "report"
    run = _wait_run(client, space_id, {"done", "failed"})
    assert run["status"] == "done", run

    r = client.get(f"/api/v1/spaces/{space_id}/feedback/reports")
    reports = r.json()["reports"]
    assert len(reports) == 1
    report = reports[0]
    assert report["status"] == "draft" and "improvement report" in report["title"]
    assert "Suggested actions" in report["body"]

    # Publish: writes a markdown file under DATA_DIR/reports and stores its
    # path in doc_url
    r = client.post(
        f"/api/v1/spaces/{space_id}/feedback/reports/{report['id']}/publish"
    )
    assert r.status_code == 200, r.text
    published = r.json()["report"]
    assert published["status"] == "published" and published["doc_url"]
    from pathlib import Path

    report_file = Path(published["doc_url"])
    assert report_file.is_file()
    assert str(tmp_path / "data" / "reports") in published["doc_url"]
    content = report_file.read_text("utf-8")
    assert report["title"] in content and "Suggested actions" in content

    # An already-published report cannot be published again → 409
    r = client.post(
        f"/api/v1/spaces/{space_id}/feedback/reports/{report['id']}/publish"
    )
    assert r.status_code == 409

    # Nonexistent suggestion → 404
    r = client.post(
        f"/api/v1/spaces/{space_id}/feedback/report",
        json={"suggestion_ids": ["nonexistent"]},
    )
    assert r.status_code == 404
