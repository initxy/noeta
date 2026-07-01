import { mergeEvents } from "../../domain/events.js";
import { connectionLabel as labelForConnection } from "../../app/connection.js";
import { collectArtifacts, traceRows } from "./projection.js";

function connectionLabel(state) {
  return labelForConnection(state);
}

function hasRealDom() {
  return (
    typeof document !== "undefined" &&
    typeof document.createTextNode === "function" &&
    typeof document.getElementById === "function"
  );
}

async function boot() {
  const root = document.getElementById("root");
  if (!root) return;
  const [{ default: React }, { createRoot }, { TraceApp }] = await Promise.all([
    import("react"),
    import("react-dom/client"),
    import("../../app/TraceApp.jsx"),
    import("../../styles/app.css"),
  ]);
  createRoot(root).render(
    React.createElement(
      React.StrictMode,
      null,
      React.createElement(TraceApp),
    ),
  );
}

if (hasRealDom()) boot();

export { collectArtifacts, connectionLabel, mergeEvents, traceRows };
