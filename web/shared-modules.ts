// The modules the page shares with plugins' web parts (index.html's import
// map): for each, a small ES module at shared/<name>.js re-exporting what the
// page itself runs (src/plugins/shared.ts puts it on globalThis before any
// plugin loads), so a plugin gets the page's own React, not a second copy.
// Served by the dev server and written into the build.
import { build } from "esbuild";
import { readFileSync } from "node:fs";
import { createRequire } from "node:module";
import { join } from "node:path";
import type { Plugin } from "vite";

/** The same list as @initxy/noeta-web-sdk's SHARED_MODULES. */
export const SHARED = ["react", "react-dom", "react-dom/client", "react/jsx-runtime", "react/jsx-dev-runtime", "@initxy/noeta-web-sdk", "@initxy/noeta-ui"];

/** Where a shared module's file is, relative to the page. */
export const sharedFile = (spec: string): string => `shared/${spec.replace(/^@/, "").replace(/\//g, "-")}.js`;

/** The import map index.html must hold, byte for byte (the server's content
 * security policy allows this one inline script by its hash). */
export const IMPORT_MAP = JSON.stringify({ imports: Object.fromEntries(SHARED.map((s) => [s, `./${sharedFile(s)}`])) });

const IDENTIFIER = /^[A-Za-z_$][\w$]*$/;

/** This repository's packages, by their source (what the page bundles). */
const SOURCES: Record<string, string> = { "@initxy/noeta-web-sdk": "sdk/src/index.ts", "@initxy/noeta-ui": "ui/src/index.tsx" };

/** The names `spec` exports: a CommonJS package's by loading it, one of
 * this repository's by bundling its source with esbuild. */
async function exportsOf(root: string, spec: string): Promise<string[]> {
  const source = SOURCES[spec];
  if (!source) return Object.keys(createRequire(join(root, "package.json"))(spec) as object);
  const out = await build({
    entryPoints: [join(root, source)],
    bundle: true,
    write: false,
    metafile: true,
    format: "esm",
    jsx: "automatic",
    packages: "external",
    outdir: "out",
    logLevel: "silent",
  });
  return Object.values(out.metafile.outputs).flatMap((o) => o.exports);
}

/** The shim module of `spec`, exporting `names`. */
export function shim(spec: string, names: string[]): string {
  const named = [...new Set(names)].filter((n) => n !== "default" && n !== "__esModule" && IDENTIFIER.test(n)).sort();
  return [
    `// The page's own ${spec}, for plugins (web/shared-modules.ts).`,
    `const m = globalThis.__noetaShared?.[${JSON.stringify(spec)}];`,
    `if (!m) throw new Error(${JSON.stringify(`noeta: the page has not provided ${spec}`)});`,
    "export default m.default ?? m;",
    named.length ? `export const { ${named.join(", ")} } = m;` : "",
    "",
  ].join("\n");
}

export function sharedModules(): Plugin {
  let root = process.cwd();
  let files: Promise<Map<string, string>> | null = null;
  const shims = () =>
    (files ??= Promise.all(SHARED.map(async (s): Promise<[string, string]> => [sharedFile(s), shim(s, await exportsOf(root, s))])).then(
      (pairs) => new Map(pairs),
    ));
  return {
    name: "noeta-shared-modules",
    configResolved(config) {
      root = config.root;
      const html = readFileSync(join(root, "index.html"), "utf-8");
      if (!html.includes(`<script type="importmap">${IMPORT_MAP}</script>`)) {
        throw new Error(`index.html must hold <script type="importmap">${IMPORT_MAP}</script>`);
      }
    },
    configureServer(server) {
      server.middlewares.use((req, res, next) => {
        const path = (req.url ?? "").split("?")[0].replace(/^\//, "");
        void shims().then((all) => {
          const body = all.get(path);
          if (body === undefined) return next();
          res.setHeader("content-type", "text/javascript");
          res.end(body);
        }, next);
      });
    },
    async generateBundle() {
      for (const [fileName, source] of await shims()) this.emitFile({ type: "asset", fileName, source });
    },
  };
}
