// Builds the web part into one ES module (dist/index.js). The modules the
// page provides stay imports: its import map serves the page's own.
import { build } from "esbuild";

const SHARED = ["react", "react-dom", "react-dom/client", "react/jsx-runtime", "react/jsx-dev-runtime", "@initxy/web-sdk", "@initxy/ui"];

await build({
  entryPoints: ["src/index.tsx"],
  outfile: "dist/index.js",
  bundle: true,
  format: "esm",
  jsx: "automatic",
  target: "es2022",
  external: SHARED,
  legalComments: "none",
  logLevel: "info",
});
