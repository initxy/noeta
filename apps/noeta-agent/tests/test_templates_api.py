"""Template / workflow-template CRUD endpoints.

Covers: the full CRUD chain, owner/member permissions, name conflict 409,
placeholder soft warnings, node reference validation, deleting a referenced
template 409.
"""
from __future__ import annotations

from tests.conftest import login, personal_space_id


def _create_template(client, space_id, name="Tracking design", **over):
    body = {
        "name": name,
        "description": "Design tracking based on the requirements document",
        "prompt": "Design tracking based on {requirements_doc_url}; business line: {business_line}",
        "params": [
            {"name": "requirements_doc_url",
             "description": "URL of the requirements document", "required": True},
            {"name": "business_line", "description": "Owning business line",
             "required": False},
        ],
    }
    body.update(over)
    return client.post(f"/api/v1/spaces/{space_id}/templates", json=body)


def test_template_crud_roundtrip(client):
    login(client)
    sid = personal_space_id(client)

    # create
    r = _create_template(client, sid)
    assert r.status_code == 201, r.text
    tpl = r.json()["template"]
    assert tpl["name"] == "Tracking design"
    assert [p["name"] for p in tpl["params"]] == [
        "requirements_doc_url", "business_line"
    ]
    assert r.json()["warnings"] == []

    # list
    r = client.get(f"/api/v1/spaces/{sid}/templates")
    assert r.status_code == 200
    assert [t["name"] for t in r.json()["templates"]] == ["Tracking design"]

    # patch
    r = client.patch(
        f"/api/v1/spaces/{sid}/templates/{tpl['id']}",
        json={"description": "updated description"},
    )
    assert r.status_code == 200, r.text
    assert r.json()["template"]["description"] == "updated description"

    # delete
    r = client.delete(f"/api/v1/spaces/{sid}/templates/{tpl['id']}")
    assert r.status_code == 200
    assert client.get(f"/api/v1/spaces/{sid}/templates").json()["templates"] == []


def test_template_placeholder_warnings(client):
    login(client)
    sid = personal_space_id(client)
    r = _create_template(
        client, sid, name="Placeholder warnings",
        prompt="Design based on {requirements_doc_url} and {nonexistent_placeholder}",
        params=[{"name": "requirements_doc_url", "description": ""},
                {"name": "unused_param", "description": ""}],
    )
    assert r.status_code == 201, r.text
    warnings = r.json()["warnings"]
    assert any("nonexistent_placeholder" in w for w in warnings)
    assert any("unused_param" in w for w in warnings)


def test_template_validation_and_conflicts(client):
    login(client)
    sid = personal_space_id(client)

    # duplicate parameter name → 422
    r = _create_template(
        client, sid,
        params=[{"name": "a", "description": ""}, {"name": "a", "description": ""}],
    )
    assert r.status_code == 422

    # same name → 409
    assert _create_template(client, sid).status_code == 201
    assert _create_template(client, sid).status_code == 409


def test_template_permission_owner_only_writes(client):
    login(client)
    r = client.post("/api/v1/spaces", json={"name": "Team T", "description": ""})
    sid = r.json()["space"]["id"]
    client.post(f"/api/v1/spaces/{sid}/members", json={"username": "bob"})
    assert _create_template(client, sid).status_code == 201
    tpl_id = client.get(f"/api/v1/spaces/{sid}/templates").json()["templates"][0]["id"]

    # bob is a member: can read, cannot write
    client.post("/api/v1/auth/dev-login", json={"username": "bob"})
    assert client.get(f"/api/v1/spaces/{sid}/templates").status_code == 200
    assert _create_template(client, sid, name="bob's").status_code == 403
    assert client.patch(
        f"/api/v1/spaces/{sid}/templates/{tpl_id}", json={"name": "renamed"}
    ).status_code == 403
    assert client.delete(
        f"/api/v1/spaces/{sid}/templates/{tpl_id}"
    ).status_code == 403


def test_workflow_crud_and_reference_guard(client):
    login(client)
    sid = personal_space_id(client)
    t1 = _create_template(client, sid, name="Tracking requirements").json()["template"]
    t2 = _create_template(client, sid, name="Tracking design").json()["template"]

    # a node referencing a nonexistent template → 422
    r = client.post(
        f"/api/v1/spaces/{sid}/workflow-templates",
        json={"name": "Broken workflow", "nodes": [{"template_id": "nope"}]},
    )
    assert r.status_code == 422

    # create
    r = client.post(
        f"/api/v1/spaces/{sid}/workflow-templates",
        json={
            "name": "Tracking end-to-end",
            "description": "requirements → design",
            "nodes": [{"template_id": t1["id"]}, {"template_id": t2["id"]}],
        },
    )
    assert r.status_code == 201, r.text
    wf = r.json()["workflow"]
    assert [n["template_id"] for n in wf["nodes"]] == [t1["id"], t2["id"]]

    # list carries node template names
    r = client.get(f"/api/v1/spaces/{sid}/workflow-templates")
    listed = r.json()["workflows"][0]
    assert [n["template_name"] for n in listed["nodes"]] == [
        "Tracking requirements", "Tracking design"
    ]

    # deleting the referenced single-node template → 409
    r = client.delete(f"/api/v1/spaces/{sid}/templates/{t1['id']}")
    assert r.status_code == 409
    assert "Tracking end-to-end" in r.json()["detail"]

    # after patching the node list (dropping t1) the delete goes through
    r = client.patch(
        f"/api/v1/spaces/{sid}/workflow-templates/{wf['id']}",
        json={"nodes": [{"template_id": t2["id"]}]},
    )
    assert r.status_code == 200, r.text
    assert client.delete(f"/api/v1/spaces/{sid}/templates/{t1['id']}").status_code == 200

    # delete workflow
    assert client.delete(
        f"/api/v1/spaces/{sid}/workflow-templates/{wf['id']}"
    ).status_code == 200
    assert client.get(
        f"/api/v1/spaces/{sid}/workflow-templates"
    ).json()["workflows"] == []


def test_workflow_requires_at_least_one_node(client):
    login(client)
    sid = personal_space_id(client)
    r = client.post(
        f"/api/v1/spaces/{sid}/workflow-templates",
        json={"name": "Empty workflow", "nodes": []},
    )
    assert r.status_code == 422
