"""Template endpoints: space-level CRUD for single-node templates + workflow
templates.

Permissions match skills: space members can see and use them (GET), the owner
creates / updates / deletes (POST/PATCH/DELETE).
Validation: hard errors are 422 (structurally invalid), name conflicts 409;
placeholder-consistency soft warnings ride along in the response (the
``warnings`` field) without blocking the save. Deleting a single-node template
referenced by a workflow -> 409.
"""
from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Request

from noeta.agent.api.spaces import require_space_member
from noeta.agent.auth.deps import CurrentUser, get_current_user
from noeta.agent.store.spaces import ROLE_OWNER, SpaceStore
from noeta.agent.store.templates import TemplateStore
from noeta.agent.workflow.templates import (
    DESC_MAX_LEN,
    TemplateValidationError,
    normalize_params,
    normalize_workflow_nodes,
    placeholder_warnings,
    validate_template_fields,
)
from pydantic import BaseModel, Field

router = APIRouter(prefix="/spaces/{space_id}", tags=["templates"])


def _templates(request: Request) -> TemplateStore:
    return request.app.state.template_store


def _spaces(request: Request) -> SpaceStore:
    return request.app.state.space_store


def _require_owner(request: Request, space_id: str, user: CurrentUser) -> None:
    require_space_member(request, space_id, user)
    role = _spaces(request).get_member_role(space_id, user.username)
    if role != ROLE_OWNER:
        raise HTTPException(
            status_code=403, detail="only the space owner can manage templates"
        )


# ------------------------------------------------------------- single-node templates
class TemplateBody(BaseModel):
    name: str = Field(min_length=1, max_length=64)
    description: str = Field(default="", max_length=DESC_MAX_LEN)
    prompt: str = Field(min_length=1)
    params: list[dict] = Field(default_factory=list)


class TemplatePatch(BaseModel):
    name: Optional[str] = Field(default=None, min_length=1, max_length=64)
    description: Optional[str] = Field(default=None, max_length=DESC_MAX_LEN)
    prompt: Optional[str] = Field(default=None, min_length=1)
    params: Optional[list[dict]] = None


@router.get("/templates")
async def list_templates(
    space_id: str, request: Request, user: CurrentUser = Depends(get_current_user)
) -> dict:
    require_space_member(request, space_id, user)
    return {"templates": _templates(request).list_templates(space_id)}


@router.post("/templates", status_code=201)
async def create_template(
    space_id: str,
    body: TemplateBody,
    request: Request,
    user: CurrentUser = Depends(get_current_user),
) -> dict:
    _require_owner(request, space_id, user)
    store = _templates(request)
    try:
        validate_template_fields(body.name, body.prompt)
        params = normalize_params(body.params)
    except TemplateValidationError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    if store.get_template_by_name(space_id, body.name.strip()) is not None:
        raise HTTPException(
            status_code=409,
            detail=f"a template with this name already exists: {body.name}",
        )
    tpl = store.create_template(
        space_id, body.name.strip(), body.description.strip(), body.prompt, params
    )
    return {
        "template": tpl,
        "warnings": placeholder_warnings(body.prompt, params),
    }


@router.patch("/templates/{template_id}")
async def update_template(
    space_id: str,
    template_id: str,
    body: TemplatePatch,
    request: Request,
    user: CurrentUser = Depends(get_current_user),
) -> dict:
    _require_owner(request, space_id, user)
    store = _templates(request)
    tpl = store.get_template(space_id, template_id)
    if tpl is None:
        raise HTTPException(status_code=404, detail="template not found")

    name = body.name.strip() if body.name is not None else tpl["name"]
    prompt = body.prompt if body.prompt is not None else tpl["prompt"]
    try:
        validate_template_fields(name, prompt)
        params = (
            normalize_params(body.params) if body.params is not None
            else tpl["params"]
        )
    except TemplateValidationError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    if name != tpl["name"]:
        dup = store.get_template_by_name(space_id, name)
        if dup is not None and dup["id"] != template_id:
            raise HTTPException(
                status_code=409,
                detail=f"a template with this name already exists: {name}",
            )

    fields: dict[str, Any] = {"name": name, "prompt": prompt, "params": params}
    if body.description is not None:
        fields["description"] = body.description.strip()
    store.update_template(space_id, template_id, **fields)
    return {
        "template": store.get_template(space_id, template_id),
        "warnings": placeholder_warnings(prompt, params),
    }


@router.delete("/templates/{template_id}")
async def delete_template(
    space_id: str,
    template_id: str,
    request: Request,
    user: CurrentUser = Depends(get_current_user),
) -> dict:
    _require_owner(request, space_id, user)
    store = _templates(request)
    if store.get_template(space_id, template_id) is None:
        raise HTTPException(status_code=404, detail="template not found")
    referencing = store.workflows_referencing(space_id, template_id)
    if referencing:
        raise HTTPException(
            status_code=409,
            detail="the template is referenced by workflows and cannot be"
            f" deleted: {', '.join(referencing)}",
        )
    store.delete_template(space_id, template_id)
    return {"ok": True}


# ------------------------------------------------------------- workflow templates
class WorkflowBody(BaseModel):
    name: str = Field(min_length=1, max_length=64)
    description: str = Field(default="", max_length=DESC_MAX_LEN)
    nodes: list[dict] = Field(min_length=1)


class WorkflowPatch(BaseModel):
    name: Optional[str] = Field(default=None, min_length=1, max_length=64)
    description: Optional[str] = Field(default=None, max_length=DESC_MAX_LEN)
    nodes: Optional[list[dict]] = None


def _check_nodes(
    request: Request, space_id: str, nodes: list[dict]
) -> list[dict]:
    """Normalize the node list and validate that every referenced single-node
    template exists (missing -> 422)."""
    normalized = normalize_workflow_nodes(nodes)
    store = _templates(request)
    for n in normalized:
        if store.get_template(space_id, n["template_id"]) is None:
            raise HTTPException(
                status_code=422,
                detail=f"a node references a missing template: {n['template_id']}",
            )
    return normalized


@router.get("/workflow-templates")
async def list_workflows(
    space_id: str, request: Request, user: CurrentUser = Depends(get_current_user)
) -> dict:
    require_space_member(request, space_id, user)
    store = _templates(request)
    workflows = store.list_workflows(space_id)
    # Attach the template name each node references (for the frontend list /
    # picker); nodes with a dangling reference get None.
    by_id = {t["id"]: t for t in store.list_templates(space_id)}
    for wf in workflows:
        for n in wf["nodes"]:
            tpl = by_id.get(n.get("template_id"))
            n["template_name"] = tpl["name"] if tpl else None
    return {"workflows": workflows}


@router.post("/workflow-templates", status_code=201)
async def create_workflow(
    space_id: str,
    body: WorkflowBody,
    request: Request,
    user: CurrentUser = Depends(get_current_user),
) -> dict:
    _require_owner(request, space_id, user)
    store = _templates(request)
    try:
        validate_template_fields(body.name, "-")  # only validate name (workflows have no prompt)
        nodes = _check_nodes(request, space_id, body.nodes)
    except TemplateValidationError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    if store.get_workflow_by_name(space_id, body.name.strip()) is not None:
        raise HTTPException(
            status_code=409,
            detail=f"a workflow with this name already exists: {body.name}",
        )
    wf = store.create_workflow(
        space_id, body.name.strip(), body.description.strip(), nodes
    )
    return {"workflow": wf}


@router.patch("/workflow-templates/{workflow_id}")
async def update_workflow(
    space_id: str,
    workflow_id: str,
    body: WorkflowPatch,
    request: Request,
    user: CurrentUser = Depends(get_current_user),
) -> dict:
    _require_owner(request, space_id, user)
    store = _templates(request)
    wf = store.get_workflow(space_id, workflow_id)
    if wf is None:
        raise HTTPException(status_code=404, detail="workflow not found")

    name = body.name.strip() if body.name is not None else wf["name"]
    try:
        validate_template_fields(name, "-")
        nodes = (
            _check_nodes(request, space_id, body.nodes)
            if body.nodes is not None else wf["nodes"]
        )
    except TemplateValidationError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    if name != wf["name"]:
        dup = store.get_workflow_by_name(space_id, name)
        if dup is not None and dup["id"] != workflow_id:
            raise HTTPException(
                status_code=409,
                detail=f"a workflow with this name already exists: {name}",
            )

    fields: dict[str, Any] = {"name": name, "nodes": nodes}
    if body.description is not None:
        fields["description"] = body.description.strip()
    store.update_workflow(space_id, workflow_id, **fields)
    return {"workflow": store.get_workflow(space_id, workflow_id)}


@router.delete("/workflow-templates/{workflow_id}")
async def delete_workflow(
    space_id: str,
    workflow_id: str,
    request: Request,
    user: CurrentUser = Depends(get_current_user),
) -> dict:
    _require_owner(request, space_id, user)
    store = _templates(request)
    if store.get_workflow(space_id, workflow_id) is None:
        raise HTTPException(status_code=404, detail="workflow not found")
    store.delete_workflow(space_id, workflow_id)
    return {"ok": True}
