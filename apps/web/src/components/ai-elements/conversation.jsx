import { ArrowDown, Download } from "lucide-react";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { cn } from "../../lib/classnames.js";

// Within this distance (px) from the bottom counts as the user being "stuck to
// the bottom"; only then does new content auto-follow downward.
const STICK_THRESHOLD = 180;

function Conversation({ className, children, ...props }) {
  return (
    <section className={cn("ai-conversation", className)} role="log" {...props}>
      {children}
    </section>
  );
}

// Auto-stick-to-bottom container. Goals:
//   1. On switching/opening a session, appear at the bottom (latest message)
//      directly, with no visible scroll from the top down.
//   2. While streaming live, only follow if the user is still at the bottom;
//      don't yank them back when they scroll up through history.
//   3. After a lazy-loaded image in a bubble lands and grows the height, re-stick
//      if still at the bottom, so the latest message isn't pushed out of view.
// resetKey is passed activeTaskId: a change means the session switched.
//
// On a session switch, history isn't laid out at once: events enter the DOM one
// by one, markdown/code blocks render block by block, so the container grows in
// several steps over a few hundred ms. Sticking on every step makes the user
// watch it "scroll down a notch at a time" (measured: scrollHeight 808→1745 over
// 7 steps, ~650ms). Fix: after a switch, hide the container (visibility still
// participates in layout, scrollHeight is real, and we keep sticking) until the
// content goes "quiet" for a moment or the fallback cap is hit, then reveal it at
// once — so it looks like "switched straight to the bottom" with no visible scroll.
const SETTLE_QUIET_MS = 220; // content quiet this long counts as laid out
const SETTLE_CAP_MS = 1600; // fallback: force reveal however slow; never stay hidden
function ConversationContent({ className, children, resetKey, ...props }) {
  const ref = useRef(null);
  // Whether to "stick and follow". Forced on when switching sessions; off when
  // the user scrolls up through history, back on when they scroll to the bottom.
  // Stored in a ref to avoid re-rendering on every scroll.
  const stick = useRef(true);
  // Whether we're in the post-switch "laying out history" hidden phase.
  const settling = useRef(false);
  const quietTimer = useRef(0);
  const capTimer = useRef(0);
  const [hidden, setHidden] = useState(false);

  const pinToBottom = useCallback(() => {
    const node = ref.current;
    if (node) node.scrollTop = node.scrollHeight;
  }, []);

  // End the hidden layout phase: stick to bottom + reveal.
  const revealAtBottom = useCallback(() => {
    window.clearTimeout(quietTimer.current);
    window.clearTimeout(capTimer.current);
    settling.current = false;
    pinToBottom();
    setHidden(false);
  }, [pinToBottom]);

  // Track whether the user is still at the bottom — don't yank them back when
  // they scroll up through history. Ignored during the hidden layout phase.
  useEffect(() => {
    const node = ref.current;
    if (!node) return undefined;
    const onScroll = () => {
      if (settling.current) return;
      stick.current =
        node.scrollHeight - node.scrollTop - node.clientHeight <= STICK_THRESHOLD;
    };
    node.addEventListener("scroll", onScroll, { passive: true });
    return () => node.removeEventListener("scroll", onScroll);
  }, []);

  // Stick to bottom on content change. During the hidden layout phase: keep
  // sticking but stay hidden, debounce on "quiet", reveal only once laid out.
  // Outside it (live streaming): follow to the bottom if the user is still
  // stuck there. The observer isn't falsely triggered by typing in the editor
  // (which is in a separate subtree).
  //
  // WS-A — pinToBottom reads scrollHeight + writes scrollTop = a forced
  // synchronous reflow. A burst of streamed mutations used to run it once PER
  // mutation, thrashing layout. Coalesce into a SINGLE write per frame via
  // requestAnimationFrame (which still runs before paint, so the scroll position
  // is correct on screen). The quiet-debounce resets once per frame while content
  // keeps changing — identical reveal timing.
  //
  // P3 — observe childList + subtree only, NOT characterData. React re-renders
  // streamed markdown by replacing nodes (a childList mutation the observer still
  // catches), so per-character text-node edits add no useful signal — they only
  // fire the observer far more often, an extra reflow trigger per typed token.
  useEffect(() => {
    const node = ref.current;
    if (!node || typeof MutationObserver === "undefined") return undefined;
    let raf = 0;
    const flush = () => {
      raf = 0;
      if (settling.current) {
        pinToBottom();
        window.clearTimeout(quietTimer.current);
        quietTimer.current = window.setTimeout(revealAtBottom, SETTLE_QUIET_MS);
      } else if (stick.current) {
        pinToBottom();
      }
    };
    const observer = new MutationObserver(() => {
      if (raf) return;
      raf = window.requestAnimationFrame(flush);
    });
    observer.observe(node, {
      childList: true,
      subtree: true,
    });
    return () => {
      observer.disconnect();
      if (raf) window.cancelAnimationFrame(raf);
    };
  }, [pinToBottom, revealAtBottom]);

  // Images in bubbles load lazily and grow the content once they land; re-stick
  // once if outside the layout phase and still at the bottom. The img load event
  // doesn't bubble, so listen on the whole subtree in the capture phase.
  //
  // B6 — don't gate solely on stick.current: a scroll/observer race can leave it
  // stale-false while the user is actually at the bottom, and the late image then
  // pushes the newest message out of view. Re-measure the distance to the bottom
  // independently and pin when within threshold (OR stick), so a missed pin from a
  // stale flag can't happen; stick only ever adds a pin, never suppresses one.
  useEffect(() => {
    const node = ref.current;
    if (!node) return undefined;
    const onLoad = (event) => {
      if (event.target?.tagName !== "IMG" || settling.current) return;
      const dist = node.scrollHeight - node.scrollTop - node.clientHeight;
      if (stick.current || dist <= STICK_THRESHOLD) pinToBottom();
    };
    node.addEventListener("load", onLoad, true);
    return () => node.removeEventListener("load", onLoad, true);
  }, [pinToBottom]);

  // Switching sessions: an empty session has nothing to lay out, so don't hide;
  // a non-empty one enters the hidden layout phase, keeps sticking, and is
  // revealed by the MutationObserver's quiet debounce above, with a forced reveal
  // after the SETTLE_CAP_MS fallback.
  useEffect(() => {
    stick.current = true;
    window.clearTimeout(quietTimer.current);
    window.clearTimeout(capTimer.current);
    if (!resetKey) {
      settling.current = false;
      setHidden(false);
      pinToBottom();
      return undefined;
    }
    settling.current = true;
    setHidden(true);
    pinToBottom();
    capTimer.current = window.setTimeout(revealAtBottom, SETTLE_CAP_MS);
    return () => {
      window.clearTimeout(quietTimer.current);
      window.clearTimeout(capTimer.current);
    };
  }, [resetKey, pinToBottom, revealAtBottom]);

  return (
    <div
      ref={ref}
      className={cn("ai-conversation-content", className)}
      style={hidden ? { visibility: "hidden" } : undefined}
      {...props}
    >
      {children}
    </div>
  );
}

function ConversationEmptyState({
  className,
  icon,
  title = "No messages yet",
  description,
  children,
  ...props
}) {
  return (
    <div className={cn("ai-conversation-empty", className)} {...props}>
      {children || (
        <>
          {icon ? <div className="ai-empty-icon">{icon}</div> : null}
          <div>
            <h2>{title}</h2>
            {description ? <p>{description}</p> : null}
          </div>
        </>
      )}
    </div>
  );
}

function ConversationScrollButton({ targetRef, className, ...props }) {
  const [visible, setVisible] = useState(false);

  useEffect(() => {
    const node = targetRef && targetRef.current;
    if (!node) return undefined;
    const update = () => {
      setVisible(node.scrollHeight - node.scrollTop - node.clientHeight > 240);
    };
    update();
    node.addEventListener("scroll", update);
    return () => node.removeEventListener("scroll", update);
  }, [targetRef]);

  if (!visible) return null;
  return (
    <button
      className={cn("ai-scroll-button", className)}
      type="button"
      onClick={() => targetRef.current?.scrollTo({ top: targetRef.current.scrollHeight })}
      {...props}
    >
      <ArrowDown size={16} />
    </button>
  );
}

function messagesToMarkdown(messages) {
  return (messages || [])
    .map((message) => {
      const role = message.role || message.from || "message";
      const text = message.content || message.text || "";
      return `**${role}:** ${text}`;
    })
    .join("\n\n");
}

function ConversationDownload({
  messages,
  filename = "conversation.md",
  className,
  children,
  ...props
}) {
  const markdown = useMemo(() => messagesToMarkdown(messages), [messages]);
  return (
    <button
      className={cn("ai-icon-button", className)}
      type="button"
      onClick={() => {
        const blob = new Blob([markdown], { type: "text/markdown" });
        const url = URL.createObjectURL(blob);
        const link = document.createElement("a");
        link.href = url;
        link.download = filename;
        document.body.append(link);
        link.click();
        link.remove();
        URL.revokeObjectURL(url);
      }}
      {...props}
    >
      {children || <Download size={16} />}
    </button>
  );
}

export {
  Conversation,
  ConversationContent,
  ConversationDownload,
  ConversationEmptyState,
  ConversationScrollButton,
  messagesToMarkdown,
};
