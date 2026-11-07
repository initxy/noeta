// The page's own instances of the modules plugins share (index.html's import
// map resolves each to a shared/<name>.js re-exporting from here).
import * as ui from "@noeta-ai/ui";
import * as sdk from "@noeta-ai/web-sdk";
import * as React from "react";
import * as ReactDOM from "react-dom";
import * as ReactDOMClient from "react-dom/client";
import * as JSXRuntime from "react/jsx-runtime";

declare global {
  var __noetaShared: Record<string, unknown> | undefined;
}

/** Puts the shared modules where the shims find them; before any plugin loads. */
export function provideSharedModules(): void {
  globalThis.__noetaShared = {
    react: React,
    "react-dom": ReactDOM,
    "react-dom/client": ReactDOMClient,
    "react/jsx-runtime": JSXRuntime,
    "@noeta-ai/web-sdk": sdk,
    "@noeta-ai/ui": ui,
  };
}
