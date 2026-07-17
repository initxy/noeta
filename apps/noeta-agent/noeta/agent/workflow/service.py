"""Pure logic for workflow sessions: snapshot assembly, tab views, node goals.

Touches no DB / noeta / HTTP — inputs and outputs are dicts, easy to unit
test. Drive orchestration (task start, state machine) lives in
host/service.py; the handoff LLM call lives in workflow/handoff.py.
"""
from __future__ import annotations

from typing import Optional

from noeta.agent.workflow.templates import render_prompt

#: Status value in the tab view for a node that has not started yet (no
#: session_tasks row)
NODE_NOT_STARTED = "not_started"

#: Fixed section heading under which the handoff summary is appended to the
#: next node's goal
HANDOFF_SECTION_HEADER = "## Handoff summary from the previous stage"


def build_workflow_snapshot(workflow: dict, templates_by_id: dict) -> dict:
    """At session creation, snapshot the workflow definition + the referenced
    single-node template contents into a self-contained JSON.

    An in-flight session advances on the snapshot; later edits / deletions of
    the templates do not affect it. Missing referenced templates are
    validated by the caller (the API layer) beforehand; here a KeyError
    surfaces the programming error directly.
    """
    nodes = []
    for n in workflow["nodes"]:
        tpl = templates_by_id[n["template_id"]]
        nodes.append({
            "template_id": tpl["id"],
            "name": tpl["name"],
            "description": tpl["description"],
            "prompt": tpl["prompt"],
            "params": tpl["params"],
        })
    return {
        "workflow_template_id": workflow["id"],
        "name": workflow["name"],
        "nodes": nodes,
    }


def workflow_view(snapshot: dict, tasks: list[dict]) -> dict:
    """Workflow snapshot + session_tasks rows → the frontend tab-bar view
    (the workflow_update frame data)."""
    task_by_index = {t["node_index"]: t for t in tasks}
    nodes = []
    for i, n in enumerate(snapshot.get("nodes") or []):
        t = task_by_index.get(i)
        nodes.append({
            "index": i,
            "name": n.get("name", f"Node {i + 1}"),
            "description": n.get("description", ""),
            "params": n.get("params", []),
            "task_id": t["task_id"] if t else None,
            "status": t["status"] if t else NODE_NOT_STARTED,
        })
    started = [n for n in nodes if n["task_id"]]
    current_index = started[-1]["index"] if started else None
    return {
        "name": snapshot.get("name", ""),
        "workflow_template_id": snapshot.get("workflow_template_id"),
        "nodes": nodes,
        "current_index": current_index,
    }


def node_goal(
    node: dict,
    values: dict,
    handoff_summary: Optional[str],
    handoff_path: Optional[str] = None,
) -> str:
    """Node start goal = template prompt (placeholders substituted) + the
    handoff summary section at the end.

    When handoff_path is non-empty, append a hint line: the full handoff
    document is at that path and can be read with the read tool.
    """
    goal = render_prompt(node.get("prompt", ""), values)
    summary = (handoff_summary or "").strip()
    if summary:
        goal = f"{goal}\n\n{HANDOFF_SECTION_HEADER}\n\n{summary}"
    if handoff_path:
        goal += (
            f"\n\nThe full handoff document is at: `{handoff_path}`"
            " (read it with the read tool when you need more context)"
        )
    return goal


def next_node_index(snapshot: dict, tasks: list[dict]) -> Optional[int]:
    """Index of the next node to start; None when all have been started.

    Nodes start strictly in order (linear workflow), so the next node = the
    highest started index + 1.
    """
    total = len(snapshot.get("nodes") or [])
    started = {t["node_index"] for t in tasks if t.get("task_id")}
    nxt = (max(started) + 1) if started else 0
    return nxt if nxt < total else None
