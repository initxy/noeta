// The line between the page and the plugins: no host file reads a specific
// plugin's records or names a specific plugin's tool (what each built-in
// plugin shows lives in web/src/plugins/<name>/), and a built-in part uses
// only what a third-party part gets (react and `@noeta-ai/*`).
import type { PluginContext } from "@noeta-ai/web-sdk";
import { readdirSync, readFileSync, statSync } from "node:fs";
import { join, relative } from "node:path";
import { describe, expect, it } from "vitest";
import { BUILTIN_WEB_PARTS } from "./builtin";

const SRC = join(import.meta.dirname, "..");

function files(dir: string): string[] {
  return readdirSync(dir).flatMap((name) => {
    const path = join(dir, name);
    if (statSync(path).isDirectory()) return files(path);
    return /\.tsx?$/.test(name) && !/\.test\.tsx?$/.test(name) ? [path] : [];
  });
}

/** The host's own files: everything outside web/src/plugins/<name>/ but the plugins' loader. */
const HOST = [...files(join(SRC, "components")), ...files(join(SRC, "state")), join(SRC, "App.tsx"), join(SRC, "main.tsx")];

/** The slots each built-in part adds to, by activating it. */
function slotsOf(name: string): string[] {
  const slots: string[] = [];
  const ctx = {
    client: { taskId: "t", read: async () => [], watch: () => () => undefined, send: async () => ({}), answer: async () => ({}), signal: async () => ({}) },
    records: () => [],
    allRecords: () => [],
    call: async () => null,
    slots: { add: (slot: string) => (slots.push(slot), () => undefined), wait: async () => undefined },
    theme: { mode: "light", onChange: () => () => undefined },
    desktop: null,
    Transcript: () => null,
  } as unknown as PluginContext;
  BUILTIN_WEB_PARTS[name].activate(ctx);
  return slots;
}

const PLUGINS = Object.keys(BUILTIN_WEB_PARTS);
const TOOLS = [...new Set(PLUGINS.flatMap(slotsOf).flatMap((s) => (/^(tool|request):/.test(s) ? [s.split(":")[1]] : [])))];
const quoted = (word: string) => new RegExp(`["'\`]${word}["'\`]`);
const escape = (s: string) => s.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");

describe("the page and the built-in plugins", () => {
  it("knows the tools the built-in parts show", () => {
    expect(TOOLS).toEqual(expect.arrayContaining(["Bash", "Edit", "Write", "Read", "Agent", "Workflow", "ExitPlanMode", "ReportFindings", "TaskCreate"]));
  });

  it("no host file names a built-in plugin's tool", () => {
    const found = HOST.flatMap((f) => {
      const text = readFileSync(f, "utf8");
      return TOOLS.filter((t) => quoted(escape(t)).test(text)).map((t) => `${relative(SRC, f)}: ${t}`);
    });
    expect(found).toEqual([]);
  });

  it("no host file reads a plugin's records by name", () => {
    const found = HOST.flatMap((f) => {
      const text = readFileSync(f, "utf8");
      const hits = PLUGINS.filter(
        (p) => new RegExp(`["'\`]${escape(p)}/`).test(text) || new RegExp(`(extension|plugin)\\s*[!=]==?\\s*["'\`]${escape(p)}["'\`]`).test(text),
      );
      // Components show what the page folded; only the fold reads records at all.
      if (!f.includes(`${join(SRC, "state")}`) && /ExtensionRecorded/.test(text)) hits.push("ExtensionRecorded");
      return hits.map((p) => `${relative(SRC, f)}: ${p}`);
    });
    expect(found).toEqual([]);
  });

  it("a built-in part lives in its own directory and imports only what a third-party part can", () => {
    const allowed = /^(react|react\/.*|react-dom.*|@noeta-ai\/(web-sdk|ui)|\.\/.*)$/;
    for (const name of PLUGINS) {
      const dir = join(SRC, "plugins", name);
      expect(statSync(dir).isDirectory()).toBe(true);
      for (const f of files(dir)) {
        const imports = [...readFileSync(f, "utf8").matchAll(/^import[^"']*["']([^"']+)["']/gm)].map((m) => m[1]);
        expect(imports.filter((i) => !allowed.test(i)), relative(SRC, f)).toEqual([]);
      }
    }
  });

  it("no host file imports a built-in part but the plugins' list", () => {
    const found = HOST.filter((f) => /from\s+["'][./]*plugins\/(?!builtin|context|events|host|shared|usePlugins)/.test(readFileSync(f, "utf8")));
    expect(found.map((f) => relative(SRC, f))).toEqual([]);
  });
});
