// src/index.tsx
import { Button, Notice, Panel } from "@initxy/noeta-ui";
import { SLOTS } from "@initxy/noeta-web-sdk";
import { useEffect, useState } from "react";
import { Fragment, jsx, jsxs } from "react/jsx-runtime";
function WorkspacePanel({ ctx }) {
  const [found, setFound] = useState(null);
  const [error, setError] = useState("");
  const look = () => ctx.call("workspace").then(
    (w) => {
      setFound(w);
      setError(w.error ?? "");
    },
    (e) => setError(String(e instanceof Error ? e.message : e))
  );
  useEffect(() => ctx.client.watch((r) => r.event.type === "ModelReplied" && void look()), [ctx]);
  return /* @__PURE__ */ jsx(Panel, { title: "Workspace", children: /* @__PURE__ */ jsxs("div", { className: "web-panel", "data-testid": "web-panel", children: [
    found ? /* @__PURE__ */ jsxs(Fragment, { children: [
      /* @__PURE__ */ jsx("div", { className: "muted", children: found.cwd }),
      /* @__PURE__ */ jsxs("div", { "data-testid": "web-panel-count", children: [
        found.count,
        " entr",
        found.count === 1 ? "y" : "ies"
      ] }),
      /* @__PURE__ */ jsx("ul", { children: found.entries.slice(0, 12).map((e) => /* @__PURE__ */ jsx("li", { children: e }, e)) })
    ] }) : /* @__PURE__ */ jsx("div", { className: "muted", children: "What is in this session's directory?" }),
    error && /* @__PURE__ */ jsx(Notice, { tone: "error", children: error }),
    /* @__PURE__ */ jsx(Button, { onClick: () => void look(), children: found ? "Look again" : "Look" }),
    /* @__PURE__ */ jsx("div", { className: "muted", "data-testid": "web-panel-host", children: ctx.desktop ? `in the desktop app (${ctx.desktop.platform})` : "in a browser" })
  ] }) });
}
function activate(ctx) {
  const remove = ctx.slots.add(SLOTS.sidebar, { key: "workspace", mount: "keyed" }, () => /* @__PURE__ */ jsx(WorkspacePanel, { ctx }));
  return remove;
}
export {
  activate
};
