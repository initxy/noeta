import { useEffect, useRef } from "react";

/** Esc calls `onClose` wherever the focus is (a dialog opened from the
 * composer leaves it there), and goes no further: the composer behind does
 * not also stop the turn. */
export function useEscape(onClose: () => void): void {
  const close = useRef(onClose);
  close.current = onClose;
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key !== "Escape" || e.isComposing) return;
      e.stopPropagation();
      e.preventDefault();
      close.current();
    };
    window.addEventListener("keydown", onKey, true);
    return () => window.removeEventListener("keydown", onKey, true);
  }, []);
}
