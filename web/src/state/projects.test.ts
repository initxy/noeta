import { describe, expect, it } from "vitest";
import type { Project, TaskSummary } from "../api/types";
import { dayLabel, projectFor, projectList, projectWhere, scratchDay, scratchRoot, shortPath } from "./projects";

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

describe("projectWhere", () => {
  it("tells projects of one name apart by the fewest parent folders", () => {
    const where = projectWhere([
      { path: "/t/web-ux/srv/work", name: "work" },
      { path: "/t/web-feel/srv/work", name: "work" },
      { path: "/home/me/app", name: "app" },
    ]);
    expect([...where]).toEqual([
      ["/t/web-ux/srv/work", "web-ux/srv"],
      ["/t/web-feel/srv/work", "web-feel/srv"],
    ]);
  });
});

describe("shortPath", () => {
  it("keeps the last two folders of a long path", () => {
    expect(shortPath("/home/me/src/noeta/web")).toBe("…/noeta/web");
    expect(shortPath("/home/me")).toBe("/home/me");
  });
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

describe("scratch", () => {
  const root: Project = { path: "/h/.noeta/scratch", name: "Scratch", saved: false, sessions: 2, scratch: true };

  it("is the server's scratch entry; an older server's entries have no `scratch` field", () => {
    expect(scratchRoot([project("/w/a"), root])).toBe(root);
    expect(scratchRoot([project("/w/a")])).toBeUndefined();
    expect(scratchRoot(null)).toBeUndefined();
  });

  it("knows a scratch session's date directory, the root maybe through a symlink", () => {
    expect(scratchDay("/h/.noeta/scratch/2026-11-09/abcd1234", root)).toBe("2026-11-09");
    expect(scratchDay("/data/h/.noeta/scratch/2026-11-09/abcd1234", root)).toBe("2026-11-09");
    expect(scratchDay("/h/.noeta/scratch/notes/abcd1234", root)).toBeNull();
    expect(scratchDay("/w/2026-11-09/x", root)).toBeNull();
    expect(scratchDay("/h/.noeta/scratch/2026-11-09/abcd1234", undefined)).toBeNull();
  });

  it("labels a date as today, yesterday, or month and day", () => {
    const now = new Date(2026, 10, 9, 15);
    expect(dayLabel("2026-11-09", now)).toBe("Today");
    expect(dayLabel("2026-11-08", now)).toBe("Yesterday");
    expect(dayLabel("2026-10-30", now)).toBe("Oct 30");
    expect(dayLabel("2025-12-31", now)).toBe("Dec 31, 2025");
  });

  it("keeps the scratch root and scratch sessions' folders out of the projects", () => {
    const list = projectList([project("/w/a"), root], [session("/h/.noeta/scratch/2026-11-09/abcd1234", 3), session("/w/b", 1)]);
    expect(list.map((p) => p.path)).toEqual(["/w/a", "/w/b"]);
  });
});
