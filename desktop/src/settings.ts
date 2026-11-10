// What the app remembers, in `settings.json` under its user-data directory.
import { mkdirSync, readFileSync, renameSync, writeFileSync } from "node:fs";
import { dirname } from "node:path";

export interface WindowState {
  x?: number;
  y?: number;
  width: number;
  height: number;
  maximized: boolean;
}

export interface Settings {
  /** The `noeta` to run; found on PATH when unset. */
  noetaPath?: string;
  /** The project folder new sessions run in; unset: each gets a scratch folder. */
  project?: string;
  window: WindowState;
}

export const DEFAULT_WINDOW: WindowState = { width: 1280, height: 860, maximized: false };

function num(v: unknown): number | undefined {
  return typeof v === "number" && Number.isFinite(v) ? Math.round(v) : undefined;
}

/** A settings file's content, its bad or missing fields replaced by defaults
 * (and fields an older app wrote, such as `roots`, dropped: the next save
 * leaves them out). */
export function parseSettings(text: string): Settings {
  let raw: Record<string, unknown> = {};
  try {
    const v: unknown = JSON.parse(text);
    if (v && typeof v === "object" && !Array.isArray(v)) raw = v as Record<string, unknown>;
  } catch {
    // a broken file starts over
  }
  const w = (raw.window && typeof raw.window === "object" ? raw.window : {}) as Record<string, unknown>;
  const width = num(w.width);
  const height = num(w.height);
  return {
    noetaPath: typeof raw.noetaPath === "string" && raw.noetaPath ? raw.noetaPath : undefined,
    project: typeof raw.project === "string" && raw.project ? raw.project : undefined,
    window: {
      x: num(w.x),
      y: num(w.y),
      width: width && width >= 400 ? width : DEFAULT_WINDOW.width,
      height: height && height >= 300 ? height : DEFAULT_WINDOW.height,
      maximized: w.maximized === true,
    },
  };
}

export function loadSettings(path: string): Settings {
  try {
    return parseSettings(readFileSync(path, "utf-8"));
  } catch {
    return parseSettings("{}");
  }
}

export function saveSettings(path: string, s: Settings): void {
  mkdirSync(dirname(path), { recursive: true });
  const tmp = `${path}.${process.pid}.tmp`;
  writeFileSync(tmp, JSON.stringify(s, null, 2) + "\n");
  renameSync(tmp, path);
}

interface Rect {
  x: number;
  y: number;
  width: number;
  height: number;
}

/** The remembered position, dropped when no display shows its middle any more
 * (a monitor unplugged): the window then opens centered. */
export function visibleBounds(w: WindowState, displays: Rect[]): WindowState {
  if (w.x === undefined || w.y === undefined) return { ...w, x: undefined, y: undefined };
  const cx = w.x + w.width / 2;
  const cy = w.y + w.height / 2;
  const seen = displays.some((d) => cx >= d.x && cx < d.x + d.width && cy >= d.y && cy < d.y + d.height);
  return seen ? w : { ...w, x: undefined, y: undefined };
}
