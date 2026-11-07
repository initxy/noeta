import { SlotRegistry, type PluginModule } from "@noeta-ai/web-sdk";
import { useCallback, useEffect, useMemo, useReducer, useState } from "react";
import type { Api } from "../api/client";
import type { PluginInfo, PluginListing, UiEntry } from "../api/types";
import { BUILTIN_WEB_PARTS } from "./builtin";
import { pluginContext, type ThemeSource } from "./context";
import type { TaskEvents } from "./events";
import { PluginHost, type PartStatus, type WebPart } from "./host";
import { provideSharedModules } from "./shared";

/** The `ui_api` numbers this page runs web parts of. */
export const PAGE_UI_API: [number, number] = [1, 1];

/** Why the page does not load `p`'s web part; null when it does (or it has none). */
export function uiApiProblem(p: PluginInfo): string | null {
  if (!p.web) return null;
  const [lo, hi] = PAGE_UI_API;
  if (p.ui_api === null || p.ui_api < lo || p.ui_api > hi) return `its web part needs ui_api ${p.ui_api}; this page runs ${lo} to ${hi}`;
  return null;
}

/** The web parts a task with `off` switched off runs: a built-in plugin's
 * own (`BUILTIN_WEB_PARTS`), else a served module the page can run. */
export function webParts(plugins: PluginInfo[], off: string[], importEntry: (url: string) => Promise<unknown>, builtins = BUILTIN_WEB_PARTS): WebPart[] {
  const parts: WebPart[] = [];
  for (const p of plugins) {
    if (off.includes(p.name)) continue;
    const own = builtins[p.name];
    if (own && p.builtin) parts.push({ name: p.name, load: () => Promise.resolve(own) });
    else if (p.web && !uiApiProblem(p)) {
      const entry = p.web.entry;
      parts.push({ name: p.name, load: () => importEntry(entry) as Promise<PluginModule> });
    }
  }
  return parts;
}

/** Every built-in web part's plugin, as if listed (no listing to go by). */
function builtinsUnlisted(): PluginInfo[] {
  return Object.keys(BUILTIN_WEB_PARTS).map((name) => ({
    name,
    version: "",
    description: "",
    source: "",
    builtin: true,
    enabled: true,
    switchable: false,
    broken: null,
    parts: ["backend", "web"],
    ui_api: null,
    web: null,
  }));
}

/** The plugins listing, and the web parts of the task `events` follows
 * (none without a task), kept in step with what the task runs. */
export function usePlugins(
  api: Api,
  events: TaskEvents | null,
  pluginsOff: string[],
  theme: ThemeSource,
  cwd: string,
): { listing: PluginListing | null; refresh: () => Promise<void>; registry: SlotRegistry; status: (name: string) => PartStatus | undefined } {
  const registry = useMemo(() => new SlotRegistry(), []);
  const [listing, setListing] = useState<PluginListing | null>(null);
  const [host, setHost] = useState<PluginHost | null>(null);
  const [, changed] = useReducer((n: number) => n + 1, 0);

  const [unlisted, setUnlisted] = useState(false);
  const refresh = useCallback(
    () =>
      api.plugins(cwd || undefined).then(
        (got) => {
          setListing(got);
          setUnlisted(false);
        },
        (e: unknown) => {
          console.warn("could not list the plugins", e);
          setUnlisted(true);
        },
      ),
    [api, cwd],
  );
  useEffect(() => void refresh(), [refresh]);

  useEffect(() => {
    if (!events) return setHost(null);
    provideSharedModules();
    const h = new PluginHost(registry, (plugin) => pluginContext(plugin, { api, events, registry, theme }), changed);
    setHost(h);
    return () => h.close();
  }, [api, events, registry, theme]);

  const off = pluginsOff.join("\n");
  useEffect(() => {
    if (!host || (!listing && !unlisted)) return;
    const importEntry = (entry: string) => import(/* @vite-ignore */ api.webEntry(entry));
    // A server that lists no plugins (an older one): the built-in parts, as the page had them.
    const plugins = listing?.plugins ?? builtinsUnlisted();
    void host.sync(webParts(plugins, off ? off.split("\n") : [], importEntry));
  }, [api, host, listing, unlisted, off]);

  return { listing, refresh, registry, status: (name) => host?.status(name) };
}

/** What the task's plugins show now, fetched again a moment after its log advances. */
export function useUiItems(api: Api, taskId: string | null, lastSeq: number): UiEntry[] {
  const [items, setItems] = useState<{ taskId: string | null; items: UiEntry[] }>({ taskId: null, items: [] });
  useEffect(() => {
    if (!taskId || !lastSeq) return;
    let live = true;
    const timer = setTimeout(() => {
      api.ui(taskId).then(
        (got) => live && setItems({ taskId, items: got }),
        () => undefined,
      );
    }, 250);
    return () => {
      live = false;
      clearTimeout(timer);
    };
  }, [api, taskId, lastSeq]);
  return items.taskId === taskId ? items.items : [];
}
