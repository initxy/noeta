// B4 — robust clipboard copy shared by the chat header and the trace inspector.
//
// Returns whether the copy actually succeeded so callers can give feedback
// (toast / inline "Copied") instead of silently no-op'ing in a non-secure context
// (file:// / sandboxed iframe) where navigator.clipboard is undefined or
// execCommand returns false. Tries the async Clipboard API first, then falls back
// to a hidden <textarea> + execCommand("copy").
async function copyText(text) {
  if (typeof navigator !== "undefined" && navigator.clipboard?.writeText) {
    try {
      await navigator.clipboard.writeText(text);
      return true;
    } catch {
      /* fall through to the execCommand path below */
    }
  }
  try {
    const el = document.createElement("textarea");
    el.value = text;
    document.body.appendChild(el);
    el.select();
    const ok = document.execCommand("copy");
    el.remove();
    return !!ok;
  } catch {
    return false;
  }
}

export { copyText };
