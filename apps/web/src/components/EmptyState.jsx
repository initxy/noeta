// V10 — one empty-state component replacing six hand-rolled variants
// (.session-empty / .pane-empty / .file-panel__empty / .file-panel__preview-empty
// / .app-iframe__empty / .mcp-panel__empty). Padding/gap/typography are fixed
// here so every empty state reads the same. `kind` picks a default icon + is
// only a hint; pass `icon` to override, `title` is required, `hint` is the grey
// subline, `action` = { label, onClick } renders a small CTA button.

import {
  Inbox,
  FileText,
  FolderOpen,
  Image as ImageIcon,
  MonitorPlay,
  Plug,
} from "lucide-react";

const KIND_ICON = {
  session: Inbox,
  pane: FileText,
  files: FolderOpen,
  preview: ImageIcon,
  app: MonitorPlay,
  mcp: Plug,
};

function EmptyState({ kind, icon, title, hint, action }) {
  const Icon = KIND_ICON[kind] || FileText;
  return (
    <div className="empty-state" role="status">
      <span className="empty-state__icon" aria-hidden="true">
        {icon || <Icon size={20} strokeWidth={1.5} />}
      </span>
      <div className="empty-state__title">{title}</div>
      {hint ? <div className="empty-state__hint">{hint}</div> : null}
      {action ? (
        <button
          type="button"
          className="empty-state__action"
          onClick={action.onClick}
        >
          {action.label}
        </button>
      ) : null}
    </div>
  );
}

export { EmptyState };
