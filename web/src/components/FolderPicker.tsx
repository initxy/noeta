import { useEffect, useState } from "react";
import { ApiError, type Api } from "../api/client";
import type { DirListing } from "../api/types";
import { basename } from "../state/projects";

function why(e: unknown, listing: boolean): string {
  if (e instanceof ApiError && e.status === 404) return listing ? "This server cannot list folders: update noeta on it." : "This server keeps no projects: update noeta on it.";
  // A 400 says what is wrong with the path (it does not exist, is not a folder).
  return String(e instanceof Error ? e.message : e);
}

/** Add a project in a browser: walk the server's folders (`GET /dirs`) or
 * type a path, then save the one shown (`onAdd`, which may refuse). */
export function FolderPicker({ api, start, onAdd, onClose }: { api: Api; start?: string; onAdd: (path: string) => Promise<unknown>; onClose: () => void }) {
  const [listing, setListing] = useState<DirListing | null>(null);
  const [typed, setTyped] = useState("");
  const [error, setError] = useState("");
  const [adding, setAdding] = useState(false);
  const go = (path?: string) =>
    api.dirs(path).then(
      (l) => {
        setListing(l);
        setTyped(l.path);
        setError("");
      },
      (e: unknown) => setError(why(e, true)),
    );
  // Once, at the start folder: later moves are the person's.
  useEffect(() => void go(start || undefined), []); // eslint-disable-line react-hooks/exhaustive-deps
  const add = async () => {
    const path = listing?.path ?? typed.trim();
    if (!path) return;
    setAdding(true);
    try {
      await onAdd(path);
    } catch (e) {
      setError(why(e, false));
    } finally {
      setAdding(false);
    }
  };
  return (
    <div className="overlay" onClick={onClose} onKeyDown={(e) => e.key === "Escape" && onClose()}>
      <div className="dialog folder-picker" role="dialog" aria-label="Add project" onClick={(e) => e.stopPropagation()}>
        <div className="card-title">Add a project folder</div>
        <form
          className="path-row"
          onSubmit={(e) => {
            e.preventDefault();
            if (typed.trim()) void go(typed.trim());
          }}
        >
          <input aria-label="Folder path" value={typed} spellCheck={false} autoFocus onChange={(e) => setTyped(e.target.value)} />
          <button className="choice" type="submit">
            Go
          </button>
        </form>
        {error && <div className="notice error">{error}</div>}
        {!listing && !error && <div className="muted">Loading…</div>}
        {listing && (
          <ul className="dir-list" aria-label="Folders">
            {listing.parent && (
              <li>
                <button className="dir" onClick={() => void go(listing.parent ?? undefined)} title={listing.parent}>
                  ..
                </button>
              </li>
            )}
            {listing.dirs.map((d) => (
              <li key={d.path}>
                <button className="dir" onClick={() => void go(d.path)} title={d.path}>
                  {d.name}
                </button>
              </li>
            ))}
            {!listing.dirs.length && <li className="muted empty">No folders inside</li>}
          </ul>
        )}
        <div className="dialog-actions">
          <button className="link" onClick={onClose}>
            Cancel
          </button>
          <button className="primary" disabled={!listing || adding} onClick={() => void add()}>
            {listing ? `Add ${basename(listing.path)}` : "Add"}
          </button>
        </div>
      </div>
    </div>
  );
}
