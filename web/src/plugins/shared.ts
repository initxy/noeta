// The page's own instances of the modules plugins share (index.html's import
// map resolves each to a shared/<name>.js re-exporting from here).
import * as ui from "@initxy/ui";
import * as sdk from "@initxy/web-sdk";
import * as React from "react";
import * as ReactDOM from "react-dom";
import * as ReactDOMClient from "react-dom/client";
import * as JSXDevRuntime from "react/jsx-dev-runtime";
import * as JSXRuntime from "react/jsx-runtime";

declare global {
  var __noetaShared: Record<string, unknown> | undefined;
}

type JsxDev = (type: unknown, props: unknown, key?: unknown, isStaticChildren?: boolean) => unknown;

/** `react/jsx-dev-runtime` as the page runs it. A production page's React
 * has no `jsxDEV` (it is undefined), so a web part built in development
 * mode gets the production `jsx` / `jsxs` under that name. */
export function devRuntime(dev: { Fragment: unknown; jsxDEV?: unknown } = JSXDevRuntime, prod: typeof JSXRuntime = JSXRuntime): Record<string, unknown> {
  if (typeof dev.jsxDEV === "function") return dev;
  const jsx = prod.jsx as JsxDev;
  const jsxs = prod.jsxs as JsxDev;
  const jsxDEV: JsxDev = (type, props, key, isStaticChildren) => (isStaticChildren ? jsxs : jsx)(type, props, key);
  return { ...dev, jsxDEV };
}

/** Puts the shared modules where the shims find them; before any plugin loads. */
export function provideSharedModules(): void {
  globalThis.__noetaShared = {
    react: React,
    "react-dom": ReactDOM,
    "react-dom/client": ReactDOMClient,
    "react/jsx-runtime": JSXRuntime,
    "react/jsx-dev-runtime": devRuntime(),
    "@initxy/web-sdk": sdk,
    "@initxy/ui": ui,
  };
}
