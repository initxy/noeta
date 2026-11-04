import { useEffect, useLayoutEffect, useRef, useState, type ClipboardEvent, type KeyboardEvent } from "react";
import type { Command } from "../api/types";

export interface Attachment {
  mediaType: string;
  data: string; // base64
  name: string;
}

/** What the text before the caret is completing: a slash command (the
 * prompt's first word) or an `@` file mention. */
export function menuQuery(text: string, caret: number): { kind: "slash" | "mention"; q: string; start: number } | null {
  const before = text.slice(0, caret);
  if (/^\/\S*$/.test(before)) return { kind: "slash", q: before.slice(1), start: 0 };
  const m = /(^|\s)@([^\s@]*)$/.exec(before);
  return m ? { kind: "mention", q: m[2], start: caret - m[2].length - 1 } : null;
}

const IMAGE_TYPES = ["image/png", "image/jpeg", "image/gif", "image/webp"];

function readImage(file: File): Promise<Attachment> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve({ mediaType: file.type, data: String(reader.result).split(",")[1] ?? "", name: file.name || "pasted image" });
    reader.onerror = () => reject(reader.error);
    reader.readAsDataURL(file);
  });
}

interface Props {
  busy: boolean;
  disabled?: boolean;
  commands: Command[];
  findFiles: (q: string) => Promise<string[]>;
  onSend: (text: string, images: Attachment[]) => void;
  onStop: () => void;
  /** Shift+Tab: the next permission mode. */
  onCycleMode?: () => void;
  placeholder?: string;
  /** Text put in the box (a rewound prompt to edit and send again); `n` tells two apart. */
  draft?: { text: string; n: number };
}

/** The prompt box: Enter sends (Shift+Enter a new line), `/` lists the
 * commands, `@` the files, pasted or dropped images go with the message,
 * Esc or the stop button interrupts. Sent while the model works, a message
 * waits in the queue for its next turn. */
export function Composer({ busy, disabled, commands, findFiles, onSend, onStop, onCycleMode, placeholder, draft }: Props) {
  const [text, setText] = useState("");
  const [images, setImages] = useState<Attachment[]>([]);
  const [caret, setCaret] = useState(0);
  const [files, setFiles] = useState<string[]>([]);
  const [active, setActive] = useState(0);
  const box = useRef<HTMLTextAreaElement>(null);
  const placeCaret = useRef<number | null>(null); // after a completion, once its text is in
  const query = menuQuery(text, caret);

  useEffect(() => {
    if (!draft) return;
    placeCaret.current = draft.text.length;
    setText(draft.text);
    setCaret(draft.text.length);
    box.current?.focus();
  }, [draft]);

  useLayoutEffect(() => {
    if (placeCaret.current !== null) box.current?.setSelectionRange(placeCaret.current, placeCaret.current);
    placeCaret.current = null;
  }, [text]);

  useEffect(() => {
    if (query?.kind !== "mention") return setFiles([]);
    let live = true;
    findFiles(query.q).then((f) => live && setFiles(f), () => live && setFiles([]));
    return () => {
      live = false;
    };
  }, [query?.kind, query?.q, findFiles]);

  const options: { label: string; hint: string; insert: string }[] =
    query?.kind === "slash"
      ? commands
          .filter((c) => c.name.toLowerCase().startsWith(query.q.toLowerCase()))
          .map((c) => ({ label: `/${c.name}`, hint: [c.argument_hint, c.description].filter(Boolean).join(" — "), insert: `/${c.name} ` }))
      : query?.kind === "mention"
        ? files.map((f) => ({ label: `@${f}`, hint: "", insert: `@${f} ` }))
        : [];

  useEffect(() => setActive(0), [options.length, query?.kind]);

  const choose = (insert: string) => {
    if (!query) return;
    const next = text.slice(0, query.start) + insert + text.slice(caret);
    const at = query.start + insert.length;
    placeCaret.current = at;
    setText(next);
    setCaret(at);
    box.current?.focus();
  };

  const send = (typed = text) => {
    const t = typed.trim();
    if ((!t && !images.length) || disabled) return;
    onSend(t, images);
    setText("");
    setImages([]);
    setCaret(0);
  };

  const onKey = (e: KeyboardEvent<HTMLTextAreaElement>) => {
    // Enter while an input method composes a word confirms the word.
    if (e.nativeEvent.isComposing || e.keyCode === 229) return;
    if (e.key === "Tab" && e.shiftKey && onCycleMode) {
      e.preventDefault();
      onCycleMode();
      return;
    }
    if (options.length) {
      if (e.key === "ArrowDown" || e.key === "ArrowUp") {
        e.preventDefault();
        setActive((a) => (a + (e.key === "ArrowDown" ? 1 : options.length - 1)) % options.length);
        return;
      }
      if (e.key === "Enter" && !e.shiftKey && query?.kind === "slash") {
        // Enter runs the highlighted command, as Claude Code's menu does,
        // with what follows the command word.
        e.preventDefault();
        send(options[active].insert.trimEnd() + text.slice(caret).replace(/^\S*/, ""));
        return;
      }
      if (e.key === "Tab" || (e.key === "Enter" && !e.shiftKey)) {
        e.preventDefault();
        choose(options[active].insert);
        return;
      }
    }
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      send();
    } else if (e.key === "Escape" && busy) {
      e.preventDefault();
      onStop();
    }
  };

  const addFiles = async (list: FileList | File[]) => {
    const found = Array.from(list).filter((f) => IMAGE_TYPES.includes(f.type));
    if (found.length) setImages([...images, ...(await Promise.all(found.map(readImage)))]);
    return found.length > 0;
  };

  const onPaste = (e: ClipboardEvent<HTMLTextAreaElement>) => {
    const pasted = Array.from(e.clipboardData.files);
    if (pasted.some((f) => IMAGE_TYPES.includes(f.type))) {
      e.preventDefault();
      void addFiles(pasted);
    }
  };

  return (
    <div
      className="composer"
      onDragOver={(e) => e.preventDefault()}
      onDrop={(e) => {
        e.preventDefault();
        void addFiles(e.dataTransfer.files);
      }}
    >
      {options.length > 0 && (
        <ul className="menu" role="listbox" aria-label={query?.kind === "slash" ? "commands" : "files"}>
          {options.slice(0, 12).map((o, i) => (
            <li
              key={o.label}
              role="option"
              aria-selected={i === active}
              className={i === active ? "active" : ""}
              onMouseDown={(e) => {
                e.preventDefault();
                choose(o.insert);
              }}
            >
              <span className="menu-label">{o.label}</span>
              {o.hint && <span className="muted"> {o.hint}</span>}
            </li>
          ))}
        </ul>
      )}
      {images.length > 0 && (
        <div className="attachments">
          {images.map((img, i) => (
            <span key={i} className="chip">
              <img src={`data:${img.mediaType};base64,${img.data}`} alt={img.name} className="chip-thumb" />
              {img.name}
              <button aria-label={`remove ${img.name}`} className="link" onClick={() => setImages(images.filter((_, j) => j !== i))}>
                ×
              </button>
            </span>
          ))}
        </div>
      )}
      <div className="composer-row">
        <textarea
          ref={box}
          aria-label="Message"
          rows={Math.min(8, Math.max(1, text.split("\n").length))}
          placeholder={placeholder ?? "Reply to noeta… (/ for commands, @ for files)"}
          value={text}
          disabled={disabled}
          onChange={(e) => {
            setText(e.target.value);
            setCaret(e.target.selectionStart);
          }}
          onSelect={(e) => setCaret(e.currentTarget.selectionStart)}
          onKeyDown={onKey}
          onPaste={onPaste}
        />
        <label className="icon-button" title="Attach an image">
          <input type="file" accept={IMAGE_TYPES.join(",")} multiple hidden onChange={(e) => e.target.files && void addFiles(e.target.files)} />
          +
        </label>
        {busy && (
          <button className="stop" aria-label="Stop" title="Stop (Esc)" onClick={onStop}>
            ■
          </button>
        )}
        <button className="send" aria-label="Send" disabled={disabled || (!text.trim() && !images.length)} onClick={() => send()}>
          ↑
        </button>
      </div>
    </div>
  );
}
