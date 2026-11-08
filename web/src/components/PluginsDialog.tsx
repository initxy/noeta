import { Badge, Button, Switch } from "@initxy/noeta-ui";
import { Slot, SLOTS, useSlotCount, type SlotRegistry } from "@initxy/noeta-web-sdk";
import { useEffect, useReducer, useRef, useState, useSyncExternalStore } from "react";
import { ApiError, type Api } from "../api/client";
import type { MarketEntry, PluginInfo, PluginListing } from "../api/types";
import type { PartStatus } from "../plugins/host";
import { uiApiProblem } from "../plugins/usePlugins";

/** What an install would fetch, shown before anything is. */
interface Install {
  name: string;
  source: string;
  commit: string;
  parts: string[];
  marketplace?: string;
}

const COMMIT = /^(.*)@([0-9a-f]{7,40})$/i;

/** The person's words for a failed install or switch; a refusal (403) says how to allow it. */
function why(e: unknown): string {
  if (e instanceof ApiError && e.status === 403) {
    return `This server does not install or remove plugins over HTTP (${e.message}). Run \`noeta plugin add\` where it runs, or start it with --allow-plugin-install.`;
  }
  if (e instanceof ApiError && e.status === 501) return `This server cannot do that: ${e.message}`;
  return e instanceof Error ? e.message : String(e);
}

/** The plugins a task runs without, as asked: what its log shows (`off`)
 * with each switch sent since (`sent`: on or off) on top. */
export function offAsAsked(off: string[], sent: ReadonlyMap<string, boolean>): string[] {
  const out = off.filter((n) => sent.get(n) !== true);
  for (const [name, on] of sent) if (!on && !out.includes(name)) out.push(name);
  return out;
}

/** The task's switches: each sent is shown as sent until the log shows
 * it, and the next is sent on top of every one not shown yet (two quick
 * switches never send a list that undoes the first). */
function useTaskSwitches(api: Api, taskId: string | null, taskOff: string[], onError: (m: string) => void) {
  const sent = useRef(new Map<string, boolean>());
  const [, changed] = useReducer((n: number) => n + 1, 0);
  useEffect(() => {
    sent.current.clear(); // another session
    changed();
  }, [taskId]);
  useEffect(() => {
    for (const [name, on] of [...sent.current]) if (taskOff.includes(name) !== on) sent.current.delete(name);
    changed();
  }, [taskOff]);
  const off = offAsAsked(taskOff, sent.current);
  const set = (name: string, on: boolean) => {
    if (!taskId) return;
    sent.current.set(name, on);
    changed();
    api.setTaskPluginsOff(taskId, offAsAsked(taskOff, sent.current)).catch((e: unknown) => {
      if (sent.current.get(name) === on) sent.current.delete(name);
      changed();
      onError(why(e));
    });
  };
  return { off, set };
}

function Row({
  p,
  api,
  taskId,
  off,
  switchTask,
  status,
  registry,
  onChanged,
  onError,
}: {
  p: PluginInfo;
  api: Api;
  taskId: string | null;
  /** What the session runs without, as asked. */
  off: string[];
  switchTask: (on: boolean) => void;
  status?: PartStatus;
  registry: SlotRegistry;
  onChanged: () => void;
  onError: (m: string) => void;
}) {
  const problems = registry.problems(p.name);
  const problem = uiApiProblem(p);
  const switchDefault = (on: boolean) => api.setPluginDefault(p.name, on).then(onChanged, (e: unknown) => onError(why(e)));
  const remove = () => {
    if (!window.confirm(`Remove the plugin ${p.name} from this machine?`)) return;
    api.removePlugin(p.name).then(onChanged, (e: unknown) => onError(why(e)));
  };
  return (
    <li className="plugin-row" data-plugin={p.name}>
      <div className="plugin-head">
        <strong>{p.name}</strong>
        {p.version && <span className="muted">{p.version}</span>}
        {p.builtin && <Badge>built-in</Badge>}
        {p.parts.map((part) => (
          <Badge key={part} tone="ok">
            {part}
          </Badge>
        ))}
      </div>
      {p.description && <div className="muted">{p.description}</div>}
      {!p.builtin && <div className="muted plugin-source">{p.source}</div>}
      {p.broken && <div className="notice error">{p.broken}</div>}
      {problem && <div className="notice error">{problem}</div>}
      {status?.state === "failed" && <div className="notice error">web part {status.error}</div>}
      {problems.map((x, i) => (
        <div key={i} className="notice warn">
          {x.kind === "render" ? `failed in ${x.slot}: ${x.message}` : x.message}
        </div>
      ))}
      {(p.switchable || !p.builtin) && (
        <div className="plugin-actions">
          {p.switchable && taskId && <Switch label="This session" checked={!off.includes(p.name)} onChange={switchTask} />}
          {p.switchable && <Switch label="New sessions" checked={p.enabled} onChange={(on) => void switchDefault(on)} />}
          {!p.builtin && (
            <Button kind="link" onClick={remove}>
              Remove
            </Button>
          )}
        </div>
      )}
    </li>
  );
}

function Confirm({ install, onInstall, onCancel }: { install: Install; onInstall: () => void; onCancel: () => void }) {
  return (
    <div className="install-confirm" role="dialog" aria-label={`Install ${install.name}`}>
      <div className="card-title">Install {install.name}?</div>
      <p className="muted">Its code runs with noeta's rights on this machine, its web part with this page's. Nothing is fetched until you install.</p>
      <dl className="install-facts">
        {install.marketplace && (
          <>
            <dt>Marketplace</dt>
            <dd>{install.marketplace}</dd>
          </>
        )}
        <dt>Source</dt>
        <dd>{install.source}</dd>
        <dt>Commit</dt>
        <dd>{install.commit || "none (a local path)"}</dd>
        <dt>Parts</dt>
        <dd>{install.parts.length ? install.parts.join(", ") : "known once installed"}</dd>
      </dl>
      <div className="choices inline">
        <Button kind="primary" onClick={onInstall}>
          Install
        </Button>
        <Button onClick={onCancel}>Cancel</Button>
      </div>
    </div>
  );
}

function Discover({ api, onInstalled, onError }: { api: Api; onInstalled: (p: PluginInfo) => void; onError: (m: string) => void }) {
  const [entries, setEntries] = useState<MarketEntry[] | null>(null);
  const [notices, setNotices] = useState<string[]>([]);
  const [source, setSource] = useState("");
  const [install, setInstall] = useState<Install | null>(null);
  const [busy, setBusy] = useState(false);
  useEffect(() => {
    api.market().then((m) => {
      setEntries(m.entries);
      setNotices(m.notices);
    }, (e: unknown) => {
      setEntries([]);
      onError(why(e));
    });
  }, [api, onError]);
  const go = (i: Install) => {
    setBusy(true);
    const done = i.marketplace ? api.installPlugin(i.name, i.marketplace, i.commit) : api.installPlugin(i.source);
    done.then(
      (p) => {
        setInstall(null);
        setSource("");
        onInstalled(p);
      },
      (e: unknown) => onError(why(e)),
    ).finally(() => setBusy(false));
  };
  if (install) return <Confirm install={install} onInstall={() => !busy && go(install)} onCancel={() => setInstall(null)} />;
  return (
    <div className="discover">
      {notices.map((n) => <div key={n} className="muted plugin-notice">{n}</div>)}
      {entries === null && <div className="muted">Loading…</div>}
      {entries?.length === 0 && <div className="muted">No marketplace lists a plugin.</div>}
      <ul className="plugin-list">
        {entries?.map((e) => (
          <li key={`${e.marketplace}/${e.name}`} className="plugin-row" data-plugin={e.name}>
            <div className="plugin-head">
              <strong>{e.name}</strong>
              {e.version && <span className="muted">{e.version}</span>}
              {e.parts.map((part) => (
                <Badge key={part} tone="ok">
                  {part}
                </Badge>
              ))}
            </div>
            {e.description && <div className="muted">{e.description}</div>}
            <div className="muted plugin-source">from {e.marketplace}</div>
            <div className="plugin-actions">
              {e.installed ? (
                <span className="muted">Installed</span>
              ) : (
                <Button onClick={() => setInstall({ name: e.name, source: e.source, commit: e.commit, parts: e.parts, marketplace: e.marketplace })}>Install…</Button>
              )}
            </div>
          </li>
        ))}
      </ul>
      <form
        className="install-source"
        onSubmit={(ev) => {
          ev.preventDefault();
          const s = source.trim();
          if (!s) return;
          const m = COMMIT.exec(s);
          const name = (m ? m[1] : s).replace(/\/+$/, "").split(/[/:]/).pop()?.replace(/\.git$/, "") || s;
          setInstall({ name, source: s, commit: m ? m[2] : "", parts: [] });
        }}
      >
        <label>
          Install from a source
          <input value={source} placeholder="https://github.com/you/plugin.git@<commit>, or a path" onChange={(e) => setSource(e.target.value)} />
        </label>
        <Button type="submit" disabled={!source.trim()}>
          Review…
        </Button>
      </form>
    </div>
  );
}

/** The plugins: the ones installed (switched per session and for new
 * sessions, removed), the ones to discover, and what plugins add to the
 * settings. */
export function PluginsDialog({
  api,
  listing,
  refresh,
  taskId,
  taskOff,
  status,
  registry,
  onClose,
}: {
  api: Api;
  listing: PluginListing | null;
  refresh: () => Promise<void>;
  taskId: string | null;
  taskOff: string[];
  status: (name: string) => PartStatus | undefined;
  registry: SlotRegistry;
  onClose: () => void;
}) {
  const [tab, setTab] = useState<"installed" | "discover" | "settings">("installed");
  const [error, setError] = useState("");
  const [note, setNote] = useState("");
  const switches = useTaskSwitches(api, taskId, taskOff, setError);
  useSyncExternalStore(registry.subscribe, () => registry.version);
  const settings = useSlotCount(SLOTS.settings);
  const tabs: [typeof tab, string][] = [
    ["installed", "Installed"],
    ["discover", "Discover"],
    ...(settings ? [["settings", "Settings"] as [typeof tab, string]] : []),
  ];
  return (
    <div className="overlay" onClick={onClose}>
      <div className="dialog plugins-dialog" role="dialog" aria-label="Plugins" onClick={(e) => e.stopPropagation()}>
        <div className="card-title">Plugins</div>
        <div className="tabs" role="tablist">
          {tabs.map(([id, label]) => (
            <button key={id} role="tab" aria-selected={tab === id} className={tab === id ? "tab active" : "tab"} onClick={() => setTab(id)}>
              {label}
            </button>
          ))}
        </div>
        {error && (
          <div className="notice error" onClick={() => setError("")}>
            {error}
          </div>
        )}
        {note && <div className="notice">{note}</div>}
        {tab === "installed" && (
          <ul className="plugin-list">
            {!listing && <li className="muted">Loading…</li>}
            {listing?.plugins.map((p) => (
              <Row
                key={p.name}
                p={p}
                api={api}
                taskId={taskId}
                off={switches.off}
                switchTask={(on) => switches.set(p.name, on)}
                status={status(p.name)}
                registry={registry}
                onChanged={() => void refresh()}
                onError={setError}
              />
            ))}
          </ul>
        )}
        {tab === "discover" && (
          <Discover
            api={api}
            onError={setError}
            onInstalled={(p) => {
              setError("");
              setNote(p.broken ? `Installed ${p.name}: ${p.broken}` : `Installed ${p.name}. New sessions run it.`);
              void refresh();
              setTab("installed");
            }}
          />
        )}
        {tab === "settings" && (
          <div className="plugin-settings">
            <Slot name={SLOTS.settings} />
          </div>
        )}
        <button className="link" onClick={onClose}>
          Close
        </button>
      </div>
    </div>
  );
}
