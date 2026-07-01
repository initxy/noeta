import { CornerDownLeft, Loader2, Square, X } from "lucide-react";
import {
  createContext,
  useCallback,
  useContext,
  useMemo,
  useRef,
  useState,
} from "react";
import { cn } from "../../lib/classnames.js";

const PromptInputContext = createContext(null);

function PromptInputProvider({
  children,
  initialInput = "",
  onValueChange,
  value: controlledValue,
}) {
  const [internalValue, setInternalValue] = useState(initialInput);
  const controlled = controlledValue != null;
  const value = controlled ? controlledValue : internalValue;
  const setValue = useCallback(
    (next) => {
      if (controlled) onValueChange?.(next);
      else setInternalValue(next);
    },
    [controlled, onValueChange],
  );
  const controller = useMemo(
    () => ({
      textInput: {
        value,
        setInput: setValue,
        clear: () => setValue(""),
      },
    }),
    [setValue, value],
  );
  return (
    <PromptInputContext.Provider value={controller}>
      {children}
    </PromptInputContext.Provider>
  );
}

function usePromptInputController() {
  const ctx = useContext(PromptInputContext);
  if (!ctx) throw new Error("PromptInputProvider is required");
  return ctx;
}

function PromptInput({ className, onSubmit, children, ...props }) {
  const controller = useContext(PromptInputContext);
  const [localText, setLocalText] = useState("");
  const value = controller ? controller.textInput.value : localText;
  const setValue = controller ? controller.textInput.setInput : setLocalText;
  const clear = controller ? controller.textInput.clear : () => setLocalText("");

  const context = useMemo(
    () => ({ value, setInput: setValue, clear }),
    [value, setValue, clear],
  );

  const handleSubmit = useCallback(
    async (event) => {
      event.preventDefault();
      const text = value.trim();
      if (!text) return;
      const submitted = await onSubmit?.({ text, files: [] }, event);
      if (submitted !== false) clear();
    },
    [clear, onSubmit, value],
  );

  return (
    <PromptInputContext.Provider value={{ textInput: context }}>
      <form className={cn("ai-prompt-input", className)} onSubmit={handleSubmit} {...props}>
        {children}
      </form>
    </PromptInputContext.Provider>
  );
}

function PromptInputTextarea({ className, inputRef, onKeyDown, onChange, ...props }) {
  const controller = usePromptInputController();
  // B7 — track IME composition ourselves. nativeEvent.isComposing flips to false
  // the instant a candidate commits, so on some Safari / third-party CJK IMEs the
  // Enter that confirms a candidate leaks through as a submit. Block Enter while
  // composing AND for one frame after compositionend (the committing keydown can
  // arrive in the same tick that composition ends).
  const composingRef = useRef(false);
  const justEndedRef = useRef(false);
  return (
    <textarea
      ref={inputRef}
      className={cn("ai-prompt-textarea", className)}
      name="message"
      rows={1}
      value={controller.textInput.value}
      onChange={(event) => {
        controller.textInput.setInput(event.currentTarget.value);
        onChange?.(event);
      }}
      onCompositionStart={() => {
        composingRef.current = true;
      }}
      onCompositionEnd={() => {
        composingRef.current = false;
        justEndedRef.current = true;
        window.requestAnimationFrame(() => {
          justEndedRef.current = false;
        });
      }}
      onKeyDown={(event) => {
        onKeyDown?.(event);
        if (event.defaultPrevented) return;
        if (
          event.key === "Enter" &&
          !event.shiftKey &&
          !event.nativeEvent.isComposing &&
          !composingRef.current &&
          !justEndedRef.current
        ) {
          event.preventDefault();
          event.currentTarget.form?.requestSubmit();
        }
      }}
      {...props}
    />
  );
}

function PromptInputBody({ className, children, ...props }) {
  return (
    <div className={cn("ai-prompt-body", className)} {...props}>
      {children}
    </div>
  );
}

function PromptInputFooter({ className, children, ...props }) {
  return (
    <div className={cn("ai-prompt-footer", className)} {...props}>
      {children}
    </div>
  );
}

function PromptInputTools({ className, children, ...props }) {
  return (
    <div className={cn("ai-prompt-tools", className)} {...props}>
      {children}
    </div>
  );
}

function PromptInputButton({ className, tooltip, children, ...props }) {
  return (
    <button
      className={cn("ai-prompt-button", className)}
      type="button"
      title={typeof tooltip === "string" ? tooltip : undefined}
      {...props}
    >
      {children}
    </button>
  );
}

function PromptInputSubmit({
  className,
  status = "ready",
  onStop,
  // U13 — the composer Stop must read as "stop the current generation", distinct
  // from the sidebar's "stop and close the session". The caller passes the copy;
  // it surfaces as the button tooltip + aria-label while generating.
  stopTooltip = "Stop generating",
  children,
  ...props
}) {
  const generating = status === "submitted" || status === "streaming";
  const icon =
    status === "submitted" ? (
      <Loader2 className="spin" size={16} />
    ) : status === "streaming" ? (
      <Square size={14} />
    ) : status === "error" ? (
      <X size={16} />
    ) : (
      <CornerDownLeft size={16} />
    );
  return (
    <button
      aria-label={generating ? stopTooltip : "Send"}
      title={generating ? stopTooltip : undefined}
      className={cn("ai-prompt-submit", className)}
      type={generating && onStop ? "button" : "submit"}
      onClick={(event) => {
        if (generating && onStop) {
          // B15 — Safari has historically fired form submit on type="button"
          // clicks; preventDefault + returning false is belt-and-suspenders.
          event.preventDefault();
          onStop();
          return false;
        }
      }}
      {...props}
    >
      {children || icon}
    </button>
  );
}

export {
  PromptInput,
  PromptInputBody,
  PromptInputButton,
  PromptInputFooter,
  PromptInputProvider,
  PromptInputSubmit,
  PromptInputTextarea,
  PromptInputTools,
  usePromptInputController,
};
