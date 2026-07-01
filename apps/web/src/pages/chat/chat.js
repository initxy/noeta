import { reduceEvents } from "../../domain/reducer.js";
import { mergeEvents } from "../../domain/events.js";
import { connectionLabel as labelForConnection } from "../../app/connection.js";

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
  const [{ default: React }, { createRoot }, { ChatApp }] = await Promise.all([
    import("react"),
    import("react-dom/client"),
    import("../../app/ChatApp.jsx"),
    import("../../styles/app.css"),
  ]);
  createRoot(root).render(
    React.createElement(
      React.StrictMode,
      null,
      React.createElement(ChatApp),
    ),
  );
}

if (hasRealDom()) boot();

export { connectionLabel, mergeEvents, reduceEvents };
