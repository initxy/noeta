function connectionLabel(state) {
  switch (state) {
    case "connecting":
      return { label: "connecting", className: "state-connecting" };
    case "live":
      return { label: "live", className: "state-live" };
    case "reconnecting":
      return { label: "reconnecting", className: "state-reconnecting" };
    case "offline":
      return { label: "offline", className: "state-offline" };
    default:
      return { label: "idle", className: "state-idle" };
  }
}

export { connectionLabel };
