// The line between the page and the plugins: no host file reads a specific
// plugin's records or names a specific plugin's tool (what each built-in
// plugin shows lives in web/src/plugins/<name>/), and a built-in part uses
// only what a third-party part gets (react and `@initxy/*`).
import { SHARED_MODULES, type PluginContext } from "@initxy/noeta-web-sdk";
import { readdirSync, readFileSync, statSync } from "node:fs";
import { dirname, join, relative, resolve, sep } from "node:path";
import ts from "typescript";
import { describe, expect, it } from "vitest";
import { BUILTIN_WEB_PARTS } from "./builtin";

const SRC = join(import.meta.dirname, "..");

/** What a module imports, in every form (`import`, `import type`, a bare
 * `import "x"`, `export … from`, `import x = require()`, `import()` and
 * `require()`, `import("x").T`): each specifier, a relative one resolved to
 * the path it names; a computed `import()` or `require()` is `<computed>`. */
function importsOf(file: string, text = readFileSync(file, "utf8")): string[] {
  const source = ts.createSourceFile(file, text, ts.ScriptTarget.Latest, true, file.endsWith("x") ? ts.ScriptKind.TSX : ts.ScriptKind.TS);
  const found: string[] = [];
  const named = (spec: ts.Node | undefined) => {
    if (spec && ts.isStringLiteralLike(spec)) found.push(spec.text.startsWith(".") ? resolve(dirname(file), spec.text) : spec.text);
    else found.push("<computed>");
  };
  const visit = (node: ts.Node): void => {
    if (ts.isImportDeclaration(node) || ts.isExportDeclaration(node)) {
      if (node.moduleSpecifier) named(node.moduleSpecifier);
    } else if (ts.isImportEqualsDeclaration(node) && ts.isExternalModuleReference(node.moduleReference)) {
      named(node.moduleReference.expression);
    } else if (ts.isCallExpression(node) && (node.expression.kind === ts.SyntaxKind.ImportKeyword || (ts.isIdentifier(node.expression) && node.expression.text === "require"))) {
      named(node.arguments[0]);
    } else if (ts.isImportTypeNode(node) && ts.isLiteralTypeNode(node.argument)) {
      named(node.argument.literal);
    }
    ts.forEachChild(node, visit);
  };
  visit(source);
  return found;
}

/** Whether `path` (an import resolved) is `dir` or inside it. */
const inside = (path: string, dir: string) => path === dir || path.startsWith(dir + sep);

function files(dir: string): string[] {
  return readdirSync(dir).flatMap((name) => {
    const path = join(dir, name);
    if (statSync(path).isDirectory()) return files(path);
    return /\.tsx?$/.test(name) && !/\.test\.tsx?$/.test(name) ? [path] : [];
  });
}

/** The host's own files: everything of web/src but web/src/plugins/ (the
 * built-in parts, and the loader, which lists them). */
const HOST = files(SRC).filter((f) => !inside(f, join(SRC, "plugins")));

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
      // Components show what the page folded; only the fold reads records at all (api/ declares their type).
      if (!inside(f, join(SRC, "state")) && !inside(f, join(SRC, "api")) && /ExtensionRecorded/.test(text)) hits.push("ExtensionRecorded");
      return hits.map((p) => `${relative(SRC, f)}: ${p}`);
    });
    expect(found).toEqual([]);
  });

  it("sees every form of import, and where a relative one leads", () => {
    const f = join(SRC, "plugins", "todos", "index.tsx");
    const text = [
      'import a from "./../../components/Composer";',
      'import type { B } from "../fs";',
      'import "./side.css";',
      'export * from "../../state/transcript";',
      'export { c } from "react-markdown";',
      'import d = require("./own");',
      "const e = await import('../memory');",
      "const g = await import(name);",
      'const h = require("node:fs");',
      'let i: import("../shell").T;',
    ].join("\n");
    expect(importsOf(f, text).map((i) => (i.startsWith(SRC) ? relative(SRC, i) : i))).toEqual([
      "components/Composer",
      "plugins/fs",
      "plugins/todos/side.css",
      "state/transcript",
      "react-markdown",
      "plugins/todos/own",
      "plugins/memory",
      "<computed>",
      "node:fs",
      "plugins/shell",
    ]);
  });

  it("a built-in part lives in its own directory and imports only what a third-party part can", () => {
    const shared = new Set<string>(SHARED_MODULES);
    for (const name of PLUGINS) {
      const dir = join(SRC, "plugins", name);
      expect(statSync(dir).isDirectory()).toBe(true);
      for (const f of files(dir)) {
        const wrong = importsOf(f).filter((i) => !shared.has(i) && !inside(i, dir));
        expect(wrong.map((i) => (i.startsWith(SRC) ? relative(SRC, i) : i)), relative(SRC, f)).toEqual([]);
      }
    }
  });

  it("no host file imports a built-in part (only the loader lists them)", () => {
    const parts = PLUGINS.map((p) => join(SRC, "plugins", p));
    const found = HOST.flatMap((f) =>
      importsOf(f)
        .filter((i) => i === "<computed>" || parts.some((dir) => inside(i, dir)) || inside(i, join(SRC, "plugins", "builtin")))
        .map((i) => `${relative(SRC, f)}: ${i.startsWith(SRC) ? relative(SRC, i) : i}`),
    );
    expect(found).toEqual([]);
  });
});
