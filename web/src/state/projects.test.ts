import { describe, expect, it } from "vitest";
import type { Project, TaskSummary } from "../api/types";
import { projectFor, projectList } from "./projects";

const project = (path: string, saved = true): Project => ({ path, name: path.split("/").pop()!, saved, sessions: 0 });
const session = (cwd: string, updated_at = 0): TaskSummary => ({
  task_id: cwd,
  agent: "main",
  status: "waiting",
  waiting_on: "input",
  end_status: null,
  created_at: 0,
  updated_at,
  title: "",
  cwd,
  prompt: "",
});

describe("projectFor", () => {
  it("finds the same path, else one the other is through a symlinked prefix", () => {
    const listed = [project("/data/home/me/app"), project("/srv/lib")];
    expect(projectFor("/srv/lib", listed)?.path).toBe("/srv/lib");
    expect(projectFor("/home/me/app", listed)?.path).toBe("/data/home/me/app");
    expect(projectFor("/app", listed)).toBeUndefined(); // one segment is too little to tell
    expect(projectFor("/home/me/other", listed)).toBeUndefined();
  });
});

describe("projectList", () => {
  it("is the server's, then the sessions' other folders newest first, then the picked one", () => {
    const list = projectList([project("/data/home/me/app")], [session("/home/me/app", 9), session("/w/old", 1), session("/w/new", 5)], "/w/picked");
    expect(list.map((p) => p.path)).toEqual(["/data/home/me/app", "/w/new", "/w/old", "/w/picked"]);
    expect(projectList(null, [session("/w/a")]).map((p) => [p.name, p.saved])).toEqual([["a", false]]);
  });
});
