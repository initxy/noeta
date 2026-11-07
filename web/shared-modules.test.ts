// @vitest-environment node
import { readFileSync } from "node:fs";
import { join } from "node:path";
import { describe, expect, it } from "vitest";
import { SHARED_MODULES } from "./sdk/src/index";
import { IMPORT_MAP, SHARED, sharedFile, shim } from "./shared-modules";

describe("the shared modules", () => {
  it("are the SDK's list, each in index.html's import map", () => {
    expect(SHARED).toEqual([...SHARED_MODULES]);
    const html = readFileSync(join(import.meta.dirname, "index.html"), "utf-8");
    expect(html).toContain(`<script type="importmap">${IMPORT_MAP}</script>`);
    expect(JSON.parse(IMPORT_MAP).imports["react/jsx-runtime"]).toBe("./shared/react-jsx-runtime.js");
    expect(JSON.parse(IMPORT_MAP).imports["react/jsx-dev-runtime"]).toBe("./shared/react-jsx-dev-runtime.js");
    expect(sharedFile("@noeta-ai/web-sdk")).toBe("shared/noeta-ai-web-sdk.js");
  });

  it("re-export what the page put on globalThis, by name and as the default", async () => {
    const source = shim("demo", ["useThing", "default", "__esModule", "not-a-name", "useThing"]);
    expect(source).toContain("export const { useThing } = m;");
    globalThis.__noetaShared = { demo: { useThing: 42, default: "d" } };
    const mod = (await import(/* @vite-ignore */ `data:text/javascript,${encodeURIComponent(source)}`)) as { useThing: number; default: string };
    expect([mod.useThing, mod.default]).toEqual([42, "d"]);
  });
});
