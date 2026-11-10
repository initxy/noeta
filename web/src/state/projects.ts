// The projects column: which folders it lists, and what this browser
// remembers of it (the last project opened, the projects folded away).
import type { Project, TaskSummary } from "../api/types";

export function basename(path: string): string {
  return path.replace(/\/+$/, "").split("/").pop() || path || "server";
}

/** The projects to list: the server's (`null`: a server without
 * `/projects`), then the directories of the listed sessions it did not
 * name, then the one picked here if it is in neither. */
export function projectList(server: Project[] | null, sessions: TaskSummary[], picked = ""): Project[] {
  const out = [...(server ?? [])];
  const known = new Set(out.map((p) => p.path));
  const add = (path: string) => {
    if (known.has(path)) return;
    known.add(path);
    out.push({ path, name: basename(path), saved: false, sessions: 0 });
  };
  for (const s of [...sessions].sort((a, b) => b.updated_at - a.updated_at)) if (s.cwd) add(s.cwd);
  if (picked) add(picked);
  return out;
}

const PROJECT_KEY = "noeta.project";
const FOLDED_KEY = "noeta.folded";

export function loadLastProject(): string {
  try {
    return localStorage.getItem(PROJECT_KEY) ?? "";
  } catch {
    return "";
  }
}

export function saveLastProject(path: string): void {
  try {
    if (path) localStorage.setItem(PROJECT_KEY, path);
    else localStorage.removeItem(PROJECT_KEY);
  } catch {
    // per-page only
  }
}

export function loadFolded(): Set<string> {
  try {
    const v: unknown = JSON.parse(localStorage.getItem(FOLDED_KEY) ?? "[]");
    return new Set(Array.isArray(v) ? v.filter((x): x is string => typeof x === "string") : []);
  } catch {
    return new Set();
  }
}

export function saveFolded(paths: Set<string>): void {
  try {
    localStorage.setItem(FOLDED_KEY, JSON.stringify([...paths].slice(-200)));
  } catch {
    // per-page only
  }
}
