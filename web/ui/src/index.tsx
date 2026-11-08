// @initxy/ui: the web app's own pieces, styled by its stylesheet, for a
// plugin's web part to look like the rest of the page. The page serves this
// module itself (its import map): a plugin marks it external.
import type { ButtonHTMLAttributes, ReactNode } from "react";

export { Markdown } from "./Markdown";
export { DiffView, proposedDiff, ToolOutput } from "./Tool";

/** A line of muted text, or an error's. */
export function Notice({ tone = "info", children }: { tone?: "info" | "warn" | "error"; children: ReactNode }) {
  return <div className={`notice ${tone}`}>{children}</div>;
}

/** The page's button: plain, `primary`, or a `link`. */
export function Button({ kind = "plain", className, ...props }: ButtonHTMLAttributes<HTMLButtonElement> & { kind?: "plain" | "primary" | "link" }) {
  const own = kind === "primary" ? "primary" : kind === "link" ? "link" : "choice";
  return <button type="button" className={className ? `${own} ${className}` : own} {...props} />;
}

/** An on/off switch with its label. */
export function Switch({ checked, onChange, label, disabled }: { checked: boolean; onChange: (on: boolean) => void; label: string; disabled?: boolean }) {
  return (
    <label className="switch">
      <input type="checkbox" role="switch" checked={checked} disabled={disabled} onChange={(e) => onChange(e.target.checked)} />
      <span>{label}</span>
    </label>
  );
}

/** A small rounded label; `tone` colors it. */
export function Badge({ tone = "normal", children }: { tone?: "normal" | "ok" | "warn" | "error"; children: ReactNode }) {
  return <span className={`badge badge-${tone}`}>{children}</span>;
}

/** A titled panel of the sidebar column. */
export function Panel({ title, children }: { title: ReactNode; children: ReactNode }) {
  return (
    <section className="panel">
      <div className="panel-head">{title}</div>
      {children}
    </section>
  );
}
