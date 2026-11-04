"""``noeta serve``'s API as an OpenAPI 3.1 document (``GET /openapi.json``)."""

from __future__ import annotations

from typing import Any

from noeta.host.stream_json import noeta_version


def _op(summary: str, *, body: dict[str, Any] | None = None, params: tuple[str, ...] = (),
        query: tuple[str, ...] = ()) -> dict[str, Any]:
    op: dict[str, Any] = {"summary": summary, "responses": {
        "200": {"description": "OK", "content": {"application/json": {"schema": {}}}},
        "401": {"description": "Missing or wrong bearer token"},
        "404": {"description": "Unknown task"}}}
    parameters = [{"name": p, "in": "path", "required": True, "schema": {"type": "string"}}
                  for p in params]
    parameters += [{"name": q, "in": "query", "required": False, "schema": {"type": "string"}}
                   for q in query]
    if parameters:
        op["parameters"] = parameters
    if body is not None:
        op["requestBody"] = {"required": True, "content": {"application/json": {
            "schema": {"type": "object", **body}}}}
    return op


def _props(*required: str, **optional: str) -> dict[str, Any]:
    props: dict[str, Any] = {name: {"type": "string"} for name in required}
    props.update({name: {"type": kind} for name, kind in optional.items()})
    return {"properties": props, "required": list(required)}


_TASK = ("task_id",)
#: The API of ``noeta.host.http`` as an OpenAPI 3.1 document (``GET /openapi.json``).
OPENAPI: dict[str, Any] = {
    "openapi": "3.1.0",
    "info": {"title": "noeta", "version": noeta_version(),
             "description": "Durable AI agents: every task is an append-only event log."},
    "components": {"securitySchemes": {"bearer": {"type": "http", "scheme": "bearer"}}},
    "security": [{"bearer": []}],
    "paths": {
        "/tasks": {
            "post": _op("Create a task", body=_props(
                "agent", input="string", config="object", queue="string",
                dedupe_key="string", history_from="string", history_through="integer")),
            "get": _op("List tasks", query=("parent_id", "status", "limit"))},
        "/summaries": {"get": _op("Root tasks with their titles, directories, first prompts "
                                  "and whether they are archived", query=("limit", "archived"))},
        "/files": {"get": _op("Paths under a task's or a directory's tree holding q",
                              query=("q", "task_id", "cwd", "limit"))},
        "/tasks/{task_id}": {"get": _op("A task's row", params=_TASK)},
        "/tasks/{task_id}/events": {"get": _op("A task's events", params=_TASK,
                                               query=("after",))},
        "/tasks/{task_id}/pending": {"get": _op("Approvals and questions waiting on a person",
                                                params=_TASK)},
        "/tasks/{task_id}/watch": {"get": _op("Events and live output as server-sent events",
                                              params=_TASK, query=("after",))},
        "/tasks/{task_id}/messages": {"post": _op(
            "Send a message (or run a slash command)", params=_TASK,
            body=_props("input", when="string", dedupe_key="string"))},
        "/tasks/{task_id}/answers": {"post": _op(
            "Answer an approval or a question", params=_TASK,
            body=_props("request_id", decision="string", always="boolean", data="object"))},
        "/tasks/{task_id}/signals": {"post": _op(
            "Post a signal (permission_mode, config, resumed, restore_files, rename, archive)",
            params=_TASK,
            body=_props("name", data="object", dedupe_key="string"))},
        "/tasks/{task_id}/cancel": {"post": _op(
            "End the task, or with turn_only stop the turn", params=_TASK,
            body=_props(reason="string", turn_only="boolean"))},
        "/tasks/{task_id}/usage": {"get": _op("Tokens and cost of the task tree",
                                              params=_TASK)},
        "/tasks/{task_id}/context": {"get": _op("What fills the next request", params=_TASK)},
        "/tasks/{task_id}/rewind": {
            "get": _op("The prompts a rewind can go back to", params=_TASK),
            "post": _op("Rewind the conversation and/or files to before a prompt",
                        params=_TASK,
                        body={"properties": {"index": {"type": "integer"},
                                             "conversation": {"type": "boolean"},
                                             "files": {"type": "boolean"}},
                              "required": ["index"]})},
        "/commands": {"get": _op("Slash commands an agent offers",
                                 query=("agent", "task_id", "cwd"))},
        "/commands/run": {"post": _op(
            "Run a slash command without sending it: the message it stands for, or a local "
            "command's output", body=_props("text", agent="string", task_id="string",
                                             cwd="string"))},
        "/agents": {"get": _op("The agents this server runs")},
        "/models": {"get": _op("The model catalog")},
        "/openapi.json": {"get": _op("This document")},
        "/stream-json": {"post": _op(
            "Claude Code's headless protocol, one-way (JSON lines in the body and the "
            "reply); the same over a WebSocket at this path is two-way",
            query=("resume", "fork", "agent", "model", "cwd", "permission_mode", "max_turns",
                   "include_partial_messages", "replay_user_messages"))},
    },
}
