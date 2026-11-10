import { useEffect, useLayoutEffect, useRef, useState, type KeyboardEvent, type ReactNode } from "react";

/** Keys that scroll up from wherever the focus is in the conversation. */
const UP_KEYS = new Set(["ArrowUp", "PageUp", "Home"]);

/** The conversation's scroller: it follows new output while the person is at
 * the bottom. Scrolling up (the wheel, a touch, the keys, the scrollbar)
 * leaves it where they put it, and a button takes them back down; reaching
 * the bottom again follows again. `resetKey`: another conversation, which
 * opens at its end. */
export function FollowScroll({ resetKey, children }: { resetKey: string; children: ReactNode }) {
  const scroller = useRef<HTMLDivElement>(null);
  const content = useRef<HTMLDivElement>(null);
  const bottom = useRef<HTMLDivElement>(null);
  const following = useRef(true);
  const lastTop = useRef(0);
  const touched = useRef(0); // when the person last clicked or pressed a key in it: what grows then is theirs (a run opened)
  const [away, setAway] = useState(false);
  const [unseen, setUnseen] = useState(false);

  const toEnd = () => {
    const el = scroller.current;
    if (!el) return;
    el.scrollTop = el.scrollHeight;
    lastTop.current = el.scrollTop;
  };

  const leave = () => {
    const el = scroller.current;
    if (el && el.scrollHeight > el.clientHeight) following.current = false;
  };

  useLayoutEffect(() => {
    following.current = true;
    setUnseen(false);
    toEnd();
  }, [resetKey]);

  // Whatever grows (a streamed line, a card, an opened tool) keeps the end in view while following.
  useEffect(() => {
    const el = content.current;
    if (!el || typeof ResizeObserver === "undefined") return;
    const watch = new ResizeObserver(() => {
      if (following.current) toEnd();
      else if (performance.now() - touched.current > 600) setUnseen(true);
    });
    watch.observe(el);
    return () => watch.disconnect();
  }, []);

  // Whether the end is on screen: the jump button shows when it is not.
  useEffect(() => {
    const root = scroller.current;
    const end = bottom.current;
    if (!root || !end || typeof IntersectionObserver === "undefined") return;
    const seen = new IntersectionObserver(([e]) => {
      setAway(!e.isIntersecting);
      if (e.isIntersecting) setUnseen(false);
    }, { root, rootMargin: "0px 0px 24px 0px" });
    seen.observe(end);
    return () => seen.disconnect();
  }, []);

  const onScroll = () => {
    const el = scroller.current;
    if (!el) return;
    if (el.scrollHeight - el.scrollTop - el.clientHeight < 2) following.current = true;
    else if (el.scrollTop < lastTop.current - 1) following.current = false; // moved up: the scrollbar, a find
    lastTop.current = el.scrollTop;
  };

  const jump = () => {
    following.current = true;
    setUnseen(false);
    scroller.current?.scrollTo({ top: scroller.current.scrollHeight, behavior: "smooth" });
  };

  return (
    <div
      className="scroll"
      ref={scroller}
      onScroll={onScroll}
      onWheel={(e) => e.deltaY < 0 && leave()}
      onTouchMove={leave}
      onPointerDown={() => (touched.current = performance.now())}
      onKeyDown={(e: KeyboardEvent) => {
        touched.current = performance.now();
        if (UP_KEYS.has(e.key) && !(e.target instanceof HTMLTextAreaElement)) leave();
      }}
    >
      <div ref={content}>
        {children}
        <div ref={bottom} className="scroll-end" />
      </div>
      {away && (
        <div className="jump-wrap">
          <button className="jump" onClick={jump}>
            ↓ {unseen ? "New messages" : "Jump to latest"}
          </button>
        </div>
      )}
    </div>
  );
}
