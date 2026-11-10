import { useEffect, useRef } from "react";

/** Calls `run` every `everyMs` while the page is visible and every `hiddenMs`
 * while it is hidden (`null`: not at all), and once as soon as the page is
 * shown again. `everyMs` null stops it. The first call is the caller's. */
export function usePoll(run: () => void, everyMs: number | null, hiddenMs: number | null): void {
  const latest = useRef(run);
  latest.current = run;
  useEffect(() => {
    if (everyMs === null) return;
    let timer: ReturnType<typeof setTimeout> | undefined;
    const schedule = () => {
      const ms = document.hidden ? hiddenMs : everyMs;
      if (ms !== null) timer = setTimeout(tick, ms);
    };
    const tick = () => {
      latest.current();
      schedule();
    };
    const shown = () => {
      clearTimeout(timer);
      if (!document.hidden) latest.current();
      schedule();
    };
    schedule();
    document.addEventListener("visibilitychange", shown);
    return () => {
      clearTimeout(timer);
      document.removeEventListener("visibilitychange", shown);
    };
  }, [everyMs, hiddenMs]);
}
