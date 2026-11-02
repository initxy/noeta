"""``scripts/parity``: facts extraction, recording distillation, replay routing
and the comparator, on tiny synthetic requests."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import httpx

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.parity import compare, distill, facts, replay, wire  # noqa: E402

MAIN = "You are the coding agent of this test."
SUB = "You are a scout of this test."
TITLE = "You name sessions in this test."
COMPACT = "Squash everything above into a note."


def names() -> dict[str, Any]:
    return {facts.raw_kind(MAIN): "main-prompt", facts.raw_kind(SUB): "explore-prompt",
            facts.raw_kind(TITLE): "title-prompt",
            facts.raw_kind(COMPACT): "compact-instruction",
            facts.raw_kind("Today's date is 2026-10-28."): "date",
            "noeta_kinds": {"^Noeta says hello": "greeting"}}


def body(*messages: dict[str, Any], system: str = MAIN, tools: bool = True,
         **kw: Any) -> dict[str, Any]:
    return {"model": "m", "max_tokens": 100, "system": [{"type": "text", "text": system}],
            "tools": [{"name": "Bash", "description": "d", "input_schema": {
                "type": "object", "properties": {"command": {"type": "string",
                                                              "description": "x"}}}}]
            if tools else [], "messages": list(messages), "stream": True, **kw}


def user(*texts: str) -> dict[str, Any]:
    return {"role": "user", "content": [{"type": "text", "text": t} for t in texts]}


# ------------------------------------------------------------------ facts


def test_kind_ignores_run_specific_detail_and_reads_tags() -> None:
    assert facts.raw_kind("Today's date is 2026-10-28.") == \
        facts.raw_kind("Today's date is 2027-01-01.")
    assert facts.raw_kind("<total_tokens>5 left</total_tokens>") == "<total_tokens>"
    assert facts.raw_kind("<system-reminder>\n<tools-used>x</tools-used>") == \
        "<system-reminder>/<tools-used>"
    banner = facts.raw_kind("<system-reminder>\n[NOTICE]\nSomething happened")
    assert banner == facts.raw_kind("<system-reminder>\nSomething happened")


def test_parts_split_at_known_starts_and_part_tags_only() -> None:
    starts = {facts.raw_kind("Today's date is x")}
    text = "<total_tokens>1</total_tokens>\n\nToday's date is x\n\nmore\n\n<analysis>a</analysis>"
    assert facts.split_parts(text, starts) == [
        "<total_tokens>1</total_tokens>", "Today's date is x\n\nmore\n\n<analysis>a</analysis>"]


def test_schema_shape_hides_descriptions_and_keeps_structure() -> None:
    shape = facts.schema_shape({"type": "object", "description": "abc", "properties": {
        "description": {"type": "string", "description": "the field named description"}},
        "required": ["description"]})
    assert shape["description"] == {"len": 3, "sha": facts.sha("abc")}
    assert shape["properties"]["description"]["type"] == "string"
    assert "len" in shape["properties"]["description"]["description"]
    assert shape["required"] == ["description"]


def test_params_keep_shapes_not_values() -> None:
    p = facts.params({"model": "x", "max_tokens": 9, "metadata": {
        "user_id": json.dumps({"device_id": "secret", "session_id": "s"})},
        "safeguards": [{"type": "t", "classifier_context": {"cwd": "/home/me", "v": 1}}]})
    assert p["keys"] == ["max_tokens", "metadata", "safeguards"]
    assert p["metadata"]["user_id"] == {"json": {"device_id": "str", "session_id": "str"}}
    assert p["safeguards"] == [{"type": "t", "classifier_context": ["cwd", "v"]}]
    assert "secret" not in json.dumps(p)


def test_layout_tokens() -> None:
    kinds = facts.Kinds(names(), prompts=["do it"])
    lines = facts.layout([
        user("Today's date is 1", "do it"),
        {"role": "assistant", "content": [{"type": "text", "text": "ok"},
                                          {"type": "tool_use", "name": "Bash", "input": {}}]},
        {"role": "user", "content": [{"type": "tool_result", "is_error": True, "content": "x",
                                      "cache_control": {"type": "ephemeral", "ttl": "1h"}}]},
        {"role": "system", "content": "Noeta says hello"},
    ], kinds)
    assert lines == ["user: text:date | text:prompt", "assistant: text | tool_use:Bash",
                     "user: tool_result:error*1h", "system(str): text:greeting"]


def test_request_facts_registers_the_tool_set() -> None:
    toolsets: dict[str, Any] = {}
    f = facts.request_facts(body(user("hi")), {"anthropic-beta": "b2,b1"},
                            facts.Kinds(names()), toolsets)
    assert f["headers"]["betas"] == ["b1", "b2"]
    assert f["system"][0]["kind"] == "main-prompt"
    assert toolsets[f["toolset"]][0]["name"] == "Bash"


# ------------------------------------------------------------------ wire


def test_sse_round_trips_through_the_recorded_response_reader() -> None:
    content = [{"type": "thinking", "thinking": "hm"}, {"type": "text", "text": "hi"},
               {"type": "tool_use", "id": "t1", "name": "Bash", "input": {"command": "ls"}}]
    raw = b"".join(wire.sse(content, "tool_use", model="m")).decode()
    msg = wire.parse_response(raw)
    assert msg is not None and msg["stop_reason"] == "tool_use"
    assert wire.script_turn(msg)["content"] == [
        {"type": "thinking", "thinking": wire.THINKING_PLACEHOLDER},
        {"type": "text", "text": "hi"},
        {"type": "tool_use", "name": "Bash", "input": {"command": "ls"}}]
    assert wire.parse_response('{"type": "error", "error": {}}') is None
    assert wire.parse_response('{"content": [], "stop_reason": "end_turn"}') == {
        "content": [], "stop_reason": "end_turn"}


def test_request_kinds() -> None:
    kinds = facts.Kinds(names())
    assert wire.request_kind("/v1/messages/count_tokens", {}, kinds) == "count-tokens"
    assert wire.request_kind("/v1/messages", {"max_tokens": 1}, kinds) == "quota-probe"
    assert wire.request_kind("/", body(user("x"), system=TITLE, tools=False),
                             kinds) == "session-title"
    assert wire.request_kind("/", body(user("x", COMPACT)), kinds) == "compaction"
    assert wire.request_kind("/", body(user("x")), kinds) == "main"
    assert wire.request_kind("/", body(user("x"), system=SUB), kinds) == \
        "subagent:explore-prompt"
    assert wire.request_kind("/", body(user("x", COMPACT), system=SUB), kinds) == \
        "compaction:subagent"


# ------------------------------------------------------------------ distill


def test_step_markers() -> None:
    assert distill.step_marker("<command-name>/notes</command-name>\n"
                               "<command-args>v1</command-args>", "/notes v1")
    assert not distill.step_marker("<command-name>/notes</command-name>", "/other")
    assert distill.step_marker("left over textdo it", "do it")
    kinds = facts.Kinds({facts.raw_kind("Summary of it all"): "compact-summary"})
    steps = [{"say": "first"}, {"say": "second"}]
    assert distill.find_step(["first", "second"], steps, kinds) == 1
    assert distill.find_step(["Summary of it all\n\nfirst second", "first"], steps, kinds) == 0


def record(path: Path, n: int, req: dict[str, Any], reply: list[dict[str, Any]] | None,
           route: str = "/v1/messages") -> None:
    raw = b"".join(wire.sse(reply, "end_turn", model="m")).decode() if reply else ""
    path.mkdir(parents=True, exist_ok=True)
    (path / f"{n:04d}-x.json").write_text(json.dumps({
        "path": route, "headers": {"anthropic-beta": "b1"}, "body": req, "status": 200,
        "response": raw}))


def test_distill_session_steps_calls_and_script(tmp_path: Path, monkeypatch: Any) -> None:
    monkeypatch.setattr(facts, "load_names", names)
    env = "# Env\nPrimary working directory: /home/u/proj"
    rec = tmp_path / "s"
    record(rec, 1, body(user("one", env)), [{"type": "tool_use", "id": "a", "name": "Bash",
                                             "input": {"command": "cat /home/u/proj/f"}}])
    record(rec, 2, body(user("x"), system=TITLE, tools=False), [{"type": "text", "text": "T"}])
    record(rec, 3, body(user("one", env), {"role": "assistant", "content": "a"}, user("two")),
           [{"type": "text", "text": "done in /home/u/x"}])
    toolsets: dict[str, Any] = {}
    catalog: dict[str, Any] = {}
    spec = {"steps": [{"name": "a", "say": "one"}, {"name": "b", "say": "two"}]}
    contract, script = distill.distill_session("s", spec, rec, toolsets, catalog)
    rows = [(r["kind"], r["step"], r.get("call")) for r in contract["requests"]]
    assert rows == [("main", "a", 1), ("session-title", "a", None), ("main", "b", 1)]
    turns = [s["turns"] for s in script["steps"]]
    assert turns[0][0]["content"][0]["input"] == {"command": "cat {PROJECT}/f"}
    assert turns[1][0]["content"][0]["text"] == "done in {HOME}/x"
    assert "date" not in catalog and "main-prompt" in catalog


# ------------------------------------------------------------------ replay


def script() -> dict[str, Any]:
    return {"steps": [
        {"name": "a", "say": "one", "turns": [
            {"content": [{"type": "tool_use", "name": "Agent",
                          "input": {"prompt": "count {PROJECT}", "subagent_type": "Explore"}}]},
            {"content": [{"type": "text", "text": "finished"}]}]},
        {"name": "b", "say": "two", "turns": []}],
        "subagents": [{"agent": "explore-prompt", "prompt": "count {PROJECT}",
                       "turns": [{"content": [{"type": "text", "text": "3 lines"}]}]}],
        "compactions": []}


def replayer() -> replay.Replayer:
    s = script()
    return replay.Replayer(s, s["steps"], {"{PROJECT}": "/p"}, facts.Kinds(names()))


def test_replay_routes_main_steps_and_subagents() -> None:
    r = replayer()
    routing, turn = r.route(body(user("one")))
    assert routing == {"kind": "main", "step": "a", "call": 1}
    content, stop = r.materialize(turn, ["Agent"], routing)
    assert stop == "tool_use" and content[0]["name"] == "Agent"
    assert content[0]["input"] == {"prompt": "count /p", "subagent_type": "Explore"}
    assert "renamed" not in routing
    routing, turn = r.route(body(user("<env>", "count /p and report"), system="x"))
    assert routing["kind"] == "subagent:explore-prompt" and turn is not None
    routing, turn = r.route(body(user("one"), {"role": "assistant", "content": "x"}, user("two")))
    assert routing["step"] == "b" and turn is None
    content, _ = r.materialize(turn, [], routing)
    assert "no recorded turn" in content[0]["text"]
    routing, turn = r.route(body(user("one"), tools=False))
    assert routing["kind"] != "main" and turn is None


def test_endpoint_streams_the_recorded_turn() -> None:
    with replay.Endpoint(replayer()) as ep:
        resp = httpx.post(ep.url + "/v1/messages", json=body(user("one"), user("x")),
                          timeout=10)
        tokens = httpx.post(ep.url + "/v1/messages/count_tokens", json={}, timeout=10)
    msg = wire.parse_response(resp.text)
    assert msg is not None and msg["content"][0]["name"] == "Agent"
    assert tokens.json() == {"input_tokens": 1000}
    assert [e["routing"]["kind"] for e in ep.captured] == ["main", "count-tokens"]


# ------------------------------------------------------------------ compare


def test_schema_and_tool_differences() -> None:
    cc = [facts.tool_facts({"name": "Agent", "input_schema": {"type": "object", "properties": {
              "prompt": {"type": "string"}, "model": {"type": "string"}}}}),
          facts.tool_facts({"name": "Skill", "input_schema": {}}),
          facts.tool_facts({"name": "Later", "defer_loading": True, "input_schema": {}})]
    no = [facts.tool_facts({"name": "Task", "input_schema": {"type": "object", "properties": {
              "prompt": {"type": "integer"}}}}),
          facts.tool_facts({"name": "Extra", "input_schema": {}})]
    out = compare.diff_tools(cc, no, {"Agent": "Task"})
    assert "tools noeta does not offer: Skill" in out
    assert "deferred tools noeta does not offer: Later" in out
    assert "tools only noeta offers: Extra" in out
    assert "tool Agent is called Task in noeta" in out
    assert "tool Agent: parameters missing in noeta: model" in out
    assert any(d.startswith("tool Agent.prompt type: Claude Code string") for d in out)


def test_each_request_tool_list_is_compared_in_order(tmp_path: Path) -> None:
    def tool(name: str, deferred: bool = False) -> dict[str, Any]:
        return facts.tool_facts({"name": name, "input_schema": {},
                                 **({"defer_loading": True} if deferred else {})})

    cc = [tool("Agent"), tool("P", True), tool("Write")]
    no = [tool("Task"), tool("Write"), tool("P", True), tool("Extra")]
    assert compare.diff_request_tools(cc, no, {"Agent": "Task"}) == [
        "tools this request carries only in noeta: Extra",
        "tool order in the request differs: Claude Code Task, P, Write / noeta Task, Write, P"]
    assert compare.diff_request_tools([tool("W", True)], [tool("W")], {}) == [
        "tool W in the request: Claude Code defers it, noeta loads it"]
    # The second request moves P on noeta's side only; the session-wide
    # union (first-seen order) is the same, so only the per-request
    # comparison sees it.
    cdir = tmp_path / "parity" / "9.9"
    cdir.mkdir(parents=True)
    first = [tool("A"), tool("P", True), tool("B")]
    (cdir / "tools.json").write_text(json.dumps({
        "a": first, "a2": [tool("A"), tool("L", True), tool("P", True), tool("B")]}))
    (cdir / "s.json").write_text(json.dumps({"requests": [
        contract_row("a", 1, ["user: text:prompt"], toolset="a"),
        contract_row("a", 2, ["user: text:prompt"], toolset="a2")]}))
    noeta = {"toolsets": {"b": first,
                          "b2": [tool("A"), tool("L", True), tool("B"), tool("P", True)]},
             "sessions": {"s": {"requests": [
                 contract_row("a", 1, ["user: text:prompt"], toolset="b"),
                 contract_row("a", 2, ["user: text:prompt"], toolset="b2")]}}}
    report, count = compare.compare_all(cdir, noeta)
    assert count == 1 and "tool order in the request differs" in report


def test_params_headers_system_and_cache() -> None:
    assert compare.diff_params({"keys": ["a", "b"], "a": 1, "b": 2},
                               {"keys": ["a", "c"], "a": 3, "c": 0}) == [
        "top-level fields noeta does not send: b", "top-level fields only noeta sends: c",
        "a: Claude Code 1, noeta 3"]
    assert compare.diff_headers({"betas": ["x"], "anthropic-version": "1"},
                                {"betas": [], "anthropic-version": "1"}) == [
        "anthropic-beta noeta does not send: x"]
    assert compare.cache_marks([{"kind": "k", "cache": "*"}, {"kind": "k"}],
                               ["user: text:a* | text:b", "assistant: text*1h"]) == [
        "system[0]", "message[-2].inner", "message[-1]1h"]


def contract_row(step: str, call: int, layout: list[str], **kw: Any) -> dict[str, Any]:
    return {"kind": "main", "step": step, "call": call, "status": 200,
            "params": {"keys": ["max_tokens"], "max_tokens": 10},
            "headers": {"betas": [], "anthropic-version": "1"},
            "system": [{"kind": "main-prompt"}], "toolset": "t", "messages": layout, **kw}


def test_compare_all_reports_and_counts(tmp_path: Path) -> None:
    cdir = tmp_path / "parity" / "9.9"
    cdir.mkdir(parents=True)
    tool = facts.tool_facts({"name": "Bash", "input_schema": {}})
    (cdir / "tools.json").write_text(json.dumps({"t": [tool]}))
    cc_rows = [contract_row("a", 1, ["user: text:prompt | text:date"]),
               {"kind": "session-title", "step": "a", "status": 200}]
    (cdir / "s.json").write_text(json.dumps({"requests": cc_rows}))
    noeta = {"toolsets": {"t": [tool]}, "sessions": {"s": {
        "notes": ["step a: approval asked for Bash"],
        "requests": [contract_row("a", 1, ["user: text:prompt"]),
                     contract_row("a", 2, ["user: text:prompt"])]}}}
    report, count = compare.compare_all(cdir, noeta)
    assert "text kind Claude Code sends, noeta does not: date" in report
    assert "Claude Code makes a session-title request, noeta does not" in report
    assert "noeta made a main-loop call Claude Code did not make" in report
    assert "approval asked for Bash" in report
    assert "first message: Claude Code `user: text:prompt | text:date`" in report
    assert count == 5
    ids = [line.rsplit("`", 2)[1] for line in report.splitlines()
           if "session-title" in line and line.startswith("- ")]
    (tmp_path / "parity" / "deviations.md").write_text(f"- `{ids[0]}` accepted\n")
    _, count = compare.compare_all(cdir, noeta)
    assert count == 4


def test_distill_reads_answered_cards(tmp_path: Path) -> None:
    cards = tmp_path / "s.cards"
    cards.mkdir()
    (cards / "001-a.txt").write_text("# step a; answered with Escape\nthe card\n")
    assert distill.read_cards(tmp_path / "s") == [{"step": "a", "key": "Escape"}]


def test_proxy_rewrites_only_upstream_bodies(tmp_path: Path) -> None:
    from scripts.parity import record

    rec = record.Recorder(tmp_path, "http://up", model="cheap", effort={"medium": "high"})
    rec.drop_disabled_thinking = True
    body = {"model": "claude", "thinking": {"type": "disabled"},
            "output_config": {"effort": "medium"}}
    fwd, done = rec.outbound(body)
    assert fwd == {"model": "cheap", "output_config": {"effort": "high"}}
    assert done == ["model", "effort", "thinking-disabled-dropped"]
    assert body["thinking"] == {"type": "disabled"}
    idx = rec.next_index()
    rec.progress(idx, 10)
    assert json.loads((tmp_path / ".live").read_text()) == {str(idx): 10}
    rec.progress(idx, None)
    assert json.loads((tmp_path / ".live").read_text()) == {}


def test_deferred_names_read_from_the_listing_part() -> None:
    kinds = facts.Kinds(names={"noeta_kinds": {"^Deferred here": "deferred-tools"}})
    messages = [{"role": "user", "content": "hi"},
                {"role": "system", "content": [{"type": "text", "text":
                    "Deferred here, load them:\nWebFetch\nmcp__echo__echo\n\nAgent list"}]}]
    assert facts.deferred_names(messages, kinds) == ["WebFetch", "mcp__echo__echo"]
    row = facts.request_facts({"messages": messages}, {}, kinds, {})
    assert row["deferred"] == ["WebFetch", "mcp__echo__echo"]


def test_message_shapes_and_deferred_lists_are_compared() -> None:
    cc = contract_row("a", 1, ["user: text:prompt", "assistant: tool_use:Agent",
                               "user: tool_result[text]*", "user: tool_result:error"],
                      deferred=["A", "B"])
    no = contract_row("a", 1, ["user: text:prompt", "assistant: tool_use:Agent",
                               "user(str): tool_result", "user: tool_result"],
                      deferred=["B", "C"])
    out = compare.diff_layout(cc, no)
    assert ("layout", "message shape: Claude Code user: tool_result[text], noeta "
                      "user(str): tool_result") in out
    assert ("tools", "deferred-tool list: names noeta does not list: A") in out
    assert ("tools", "deferred-tool list: names only noeta lists: C") in out
    # a failed call and a cache mark are not shape differences
    assert len([d for d in out if "message shape" in d[1]]) == 1
