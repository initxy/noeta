import { ChevronLeft, ChevronRight } from "lucide-react";
import {
  Children,
  createContext,
  memo,
  useContext,
  useMemo,
  useState,
} from "react";
import { cn } from "../../lib/classnames.js";

function Message({ from = "assistant", className, children, ...props }) {
  return (
    <article
      className={cn(
        "ai-message",
        from === "user" ? "is-user" : "is-assistant",
        className,
      )}
      data-role={from}
      {...props}
    >
      {children}
    </article>
  );
}

function MessageContent({ className, children, ...props }) {
  return (
    <div className={cn("ai-message-content", className)} {...props}>
      {children}
    </div>
  );
}

function MessageActions({ className, children, ...props }) {
  return (
    <div className={cn("ai-message-actions", className)} {...props}>
      {children}
    </div>
  );
}

function MessageAction({ tooltip, label, className, children, ...props }) {
  return (
    <button
      className={cn("ai-icon-button", className)}
      type="button"
      title={tooltip || label}
      aria-label={label || tooltip}
      {...props}
    >
      {children}
    </button>
  );
}

const BranchContext = createContext(null);

function useBranch() {
  const value = useContext(BranchContext);
  if (!value) throw new Error("MessageBranch components need MessageBranch");
  return value;
}

function MessageBranch({ defaultBranch = 0, className, children, ...props }) {
  const branches = Children.toArray(children);
  const [currentBranch, setCurrentBranch] = useState(defaultBranch);
  const value = useMemo(
    () => ({
      currentBranch,
      totalBranches: branches.length,
      goToPrevious: () =>
        setCurrentBranch((index) =>
          index > 0 ? index - 1 : Math.max(0, branches.length - 1),
        ),
      goToNext: () =>
        setCurrentBranch((index) =>
          index < branches.length - 1 ? index + 1 : 0,
        ),
    }),
    [branches.length, currentBranch],
  );
  return (
    <BranchContext.Provider value={value}>
      <div className={cn("ai-message-branch", className)} {...props}>
        {children}
      </div>
    </BranchContext.Provider>
  );
}

function MessageBranchContent({ className, children, ...props }) {
  const { currentBranch } = useBranch();
  const branches = Children.toArray(children);
  return branches.map((branch, index) => (
    <div
      className={cn("ai-message-branch-content", index !== currentBranch && "is-hidden", className)}
      key={branch.key || index}
      {...props}
    >
      {branch}
    </div>
  ));
}

function MessageBranchSelector({ className, children, ...props }) {
  const { totalBranches } = useBranch();
  if (totalBranches <= 1) return null;
  return (
    <div className={cn("ai-branch-selector", className)} {...props}>
      {children}
    </div>
  );
}

function MessageBranchPrevious(props) {
  const { goToPrevious } = useBranch();
  return (
    <button className="ai-icon-button" type="button" onClick={goToPrevious} {...props}>
      <ChevronLeft size={14} />
    </button>
  );
}

function MessageBranchNext(props) {
  const { goToNext } = useBranch();
  return (
    <button className="ai-icon-button" type="button" onClick={goToNext} {...props}>
      <ChevronRight size={14} />
    </button>
  );
}

function MessageBranchPage({ className, ...props }) {
  const { currentBranch, totalBranches } = useBranch();
  return (
    <span className={cn("ai-branch-page", className)} {...props}>
      {currentBranch + 1} / {totalBranches}
    </span>
  );
}

const MessageResponse = memo(function MessageResponse({ className, children, ...props }) {
  return (
    <div className={cn("ai-response", className)} {...props}>
      {String(children == null ? "" : children)
        .split(/\n{2,}/)
        .map((paragraph, index) =>
          paragraph.trim() ? <p key={index}>{paragraph}</p> : null,
        )}
    </div>
  );
});

function MessageToolbar({ className, children, ...props }) {
  return (
    <div className={cn("ai-message-toolbar", className)} {...props}>
      {children}
    </div>
  );
}

export {
  Message,
  MessageAction,
  MessageActions,
  MessageBranch,
  MessageBranchContent,
  MessageBranchNext,
  MessageBranchPage,
  MessageBranchPrevious,
  MessageBranchSelector,
  MessageContent,
  MessageResponse,
  MessageToolbar,
};
