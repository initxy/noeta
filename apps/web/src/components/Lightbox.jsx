// source-agnostic image lightbox (shared modal).
//
// Takes one ``src`` (an image URL — a ``data:``/``blob:`` local preview, or the
// backend image route ``/tasks/{id}/images/{hash}``) plus an optional ``alt``.
// It doesn't care where the bytes come from, so chat-bubble thumbnails (issue 04)
// and workspace file-panel previews (issue 05) reuse this one component instead
// of building separate modals.
//
// Behavior: click a thumbnail to open → full-screen overlay + centered full-size
// image; close via ESC, clicking the overlay (outside the image), or the top-right
// ×. Mounted onto ``document.body`` via ``createPortal`` to escape the bubble's
// overflow/transform clipping and stacking context. Renders nothing when ``src``
// is empty (controlled: the parent clears src to close).
//
// U17 — on close, focus returns to whatever opened the lightbox (an explicit
// ``triggerRef`` if given, else whoever held focus at open time) so keyboard users
// don't get dumped on <body>. A failed image load (offline / bad hash / CORS)
// degrades to a friendly placeholder with a Retry that cache-busts, instead of a
// broken-image glyph. ``prevSrc``/``nextSrc``/``onPrev``/``onNext`` are reserved for
// a future multi-image pager — accepted now so adding it later won't change the
// signature; v1 renders no pager buttons.

import { useEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { X, ImageOff } from "lucide-react";

// eslint-disable-next-line no-unused-vars -- reserved pager interface (see header)
export function Lightbox({ src, alt, onClose, triggerRef, prevSrc, nextSrc, onPrev, onNext }) {
  const [failed, setFailed] = useState(false);
  const [bust, setBust] = useState(0);
  const restoreRef = useRef(null);
  // Keep the latest onClose reachable WITHOUT it being an effect dependency. The
  // sole caller passes a fresh inline `onClose` arrow each render, so depending on
  // it would re-run the focus effect — and fire its focus-restoring cleanup — on
  // every parent re-render while the lightbox is open (SSE churn during a live
  // session), yanking focus off the modal back onto the background trigger. The
  // effect must key ONLY on the open/close transition (`src`).
  const onCloseRef = useRef(onClose);
  onCloseRef.current = onClose;

  // Reset the load-failure + cache-bust state whenever a new image opens.
  useEffect(() => {
    setFailed(false);
    setBust(0);
  }, [src]);

  // Close on ESC + restore focus to the trigger on close. Listen on document (not
  // the modal node) so it responds without grabbing focus into the modal. Attach
  // only while open (src set); the cleanup (runs on close/unmount) returns focus.
  useEffect(() => {
    if (!src) return undefined;
    restoreRef.current = triggerRef?.current || document.activeElement;
    const onKey = (event) => {
      if (event.key === "Escape") onCloseRef.current?.();
    };
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("keydown", onKey);
      const el = restoreRef.current;
      // preventScroll: returning focus must not scroll an off-screen trigger into
      // view (which would jump the transcript the user was reading behind the modal).
      if (el && typeof el.focus === "function") el.focus({ preventScroll: true });
    };
  }, [src, triggerRef]);

  if (!src) return null;

  // Cache-bust only makes sense for http(s)/path URLs; appending a query to a
  // data:/blob: URL would corrupt it, so leave those untouched.
  const bustable = /^(https?:|\/)/.test(src);
  const displaySrc =
    bust > 0 && bustable ? `${src}${src.includes("?") ? "&" : "?"}v=${bust}` : src;

  return createPortal(
    <div
      className="lightbox-overlay"
      role="presentation"
      onClick={() => onClose?.()}
    >
      <button
        className="lightbox-close"
        type="button"
        title="Close (Esc)"
        aria-label="Close"
        onClick={() => onClose?.()}
      >
        <X size={18} />
      </button>
      {failed ? (
        <div className="lightbox-failed" onClick={(event) => event.stopPropagation()}>
          <ImageOff size={32} strokeWidth={1.5} />
          <span className="lightbox-failed__text">Failed to load image</span>
          <button
            type="button"
            className="lightbox-failed__retry"
            onClick={() => {
              setFailed(false);
              setBust((n) => n + 1);
            }}
          >
            Retry
          </button>
        </div>
      ) : (
        /* Clicking the image itself doesn't close (stopPropagation); only clicking the overlay outside the image closes. */
        <img
          className="lightbox-img"
          src={displaySrc}
          alt={alt || "Preview image"}
          onClick={(event) => event.stopPropagation()}
          onError={() => setFailed(true)}
        />
      )}
    </div>,
    document.body,
  );
}
