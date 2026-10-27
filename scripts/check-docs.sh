#!/usr/bin/env bash
# check-docs — doc hygiene as a gate; `check` runs it. Red on drift, "gc due" when cleanup is owed.
set -u
cd "$(dirname "$0")/.." || exit 1
if ! git rev-parse --git-dir >/dev/null 2>&1; then
  echo "check-docs: red — not a git repository"
  exit 1
fi

# Budgets. Raising one is the human's call, never a way to get green.
AGENTS_MAX_LINES=150         # each AGENTS.md
AGENTS_CHAIN_MAX_BYTES=32768 # an AGENTS.md plus every one above it (Codex's default load cap)
CONTEXT_MAX_LINES=300        # CONTEXT.md
ENTRY_MAX_LINES=10           # one `## ` entry of CONTEXT.md, non-blank lines, heading excluded
STALE_DAYS=30                # a live spec untouched this long is owed a look

fail=0
due=0
red() { echo "docs: $*"; fail=1; }
gc_due() { echo "gc due: $*"; due=1; }
files() { git ls-files --cached --others --exclude-standard -- "$@"; }

# The value of a frontmatter field, or empty.
field() {
  awk -v k="$2" 'NR == 1 { if ($0 != "---") exit; next }
    /^---$/ { exit }
    index($0, k ":") == 1 { sub("^" k ":[ \t]*", ""); print; exit }' "$1"
}

# One entry point. Claude Code skips AGENTS.md when a CLAUDE.md sits at or above the working directory.
while IFS= read -r f; do
  red "$f: fold it into AGENTS.md and remove it — AGENTS.md is the only entry point"
done < <(files CLAUDE.md '*/CLAUDE.md')
[ -f CLAUDE.local.md ] && echo "docs warning: CLAUDE.local.md makes Claude Code skip AGENTS.md for you"

# AGENTS.md: thin per file; the chain a tool loads stays under its cap; no maps of the tree.
while IFS= read -r f; do
  [ -f "$f" ] || continue
  n=$(wc -l <"$f" | tr -d ' ')
  [ "$n" -gt "$AGENTS_MAX_LINES" ] && red "$f: $n lines, budget $AGENTS_MAX_LINES — move what one module needs into that module's AGENTS.md"
  grep -q '[├└]──' "$f" && red "$f: a directory tree — agents list files themselves; keep only what code can't tell them"
  bytes=0
  d=$(dirname "$f")
  while :; do
    [ -f "$d/AGENTS.md" ] && bytes=$((bytes + $(wc -c <"$d/AGENTS.md")))
    [ "$d" = . ] && break
    d=$(dirname "$d")
  done
  [ "$bytes" -gt "$AGENTS_CHAIN_MAX_BYTES" ] && red "$f: $bytes bytes with the AGENTS.md files above it, cap $AGENTS_CHAIN_MAX_BYTES"
done < <(files AGENTS.md '*/AGENTS.md')

# Specs: a known status, in the right directory, named by date, ledger matching the status.
now=$(date +%s)
for f in docs/specs/*.md docs/specs/archive/*.md; do
  [ -f "$f" ] || continue
  [ "$f" = docs/specs/TEMPLATE.md ] && continue
  case ${f##*/} in
    [0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]-*.md) ;;
    *) red "$f: name it <YYYY-MM-DD>-<slug>.md" ;;
  esac
  s=$(field "$f" status)
  case $f in docs/specs/archive/*) archived=1 ;; *) archived=0 ;; esac
  case $s in
    proposed | active)
      [ $archived = 1 ] && red "$f: '$s' but in archive/ — move it back out"
      ;;
    done | superseded | dropped)
      [ $archived = 0 ] && red "$f: '$s' — flipping it and moving it into docs/specs/archive/ is one action"
      ;;
    *) red "$f: status '$s' — allowed: proposed, active, done, superseded, dropped" ;;
  esac
  if [ "$s" = superseded ] && [ -z "$(field "$f" superseded_by)" ]; then
    red "$f: superseded — name the spec that replaced it in 'superseded_by'"
  fi
  open=0
  grep -q '^[[:space:]]*- \[ \]' "$f" && open=1
  [ "$s" = done ] && [ $open = 1 ] && red "$f: done with unticked Plan tasks — tick them or reopen it"
  if [ "$s" = active ] && [ $open = 0 ] && grep -q '^[[:space:]]*- \[[xX]\]' "$f"; then
    gc_due "$f: active with every Plan task ticked — review it, or record what is left"
  fi
  if [ $archived = 0 ] && { [ "$s" = proposed ] || [ "$s" = active ]; }; then
      t=$(git log -1 --format=%ct -- "$f")
      if [ -n "$t" ] && [ $((now - t)) -gt $((STALE_DAYS * 86400)) ]; then
        gc_due "$f: '$s' and untouched for over $STALE_DAYS days — still wanted?"
      fi
  fi
done

# ADRs: Status is one line; changing a decision is a new ADR, not an amendment.
for f in docs/adr/[0-9]*.md; do
  [ -f "$f" ] || continue
  if ! grep -m1 '^- Status:' "$f" | grep -Eqx -- '- Status: (accepted|superseded by ADR-[0-9]+)'; then
    red "$f: Status must read 'accepted' or 'superseded by ADR-NNN' — to change a decision, write a new ADR"
  fi
done

# CONTEXT.md: one, at the root — a module's concepts go in its AGENTS.md, which tools load on their own.
while IFS= read -r f; do
  [ "$f" = CONTEXT.md ] && continue
  red "$f: no tool loads a module CONTEXT.md — move it into $(dirname "$f")/AGENTS.md"
done < <(files '*/CONTEXT.md')
if [ -f CONTEXT.md ]; then
  n=$(wc -l <CONTEXT.md | tr -d ' ')
  [ "$n" -gt "$CONTEXT_MAX_LINES" ] && red "CONTEXT.md: $n lines, budget $CONTEXT_MAX_LINES — trim, or move module-only entries into that module's AGENTS.md"
  while IFS= read -r entry; do
    red "CONTEXT.md: entry $entry, budget $ENTRY_MAX_LINES — point at the code instead of restating it"
  done < <(awk -v max="$ENTRY_MAX_LINES" '
    /^```/ { fence = !fence }
    !fence && /^## / { if (h != "" && n > max) print "\"" h "\" is " n " lines"; h = substr($0, 4); n = 0; next }
    h != "" && NF { n++ }
    END { if (h != "" && n > max) print "\"" h "\" is " n " lines" }' CONTEXT.md)
fi

# Pointers into the code still resolve: CONTEXT.md and accepted ADRs.
for f in CONTEXT.md docs/adr/[0-9]*.md; do
  [ -f "$f" ] || continue
  case $f in docs/adr/*) grep -q '^- Status: accepted$' "$f" || continue ;; esac
  while IFS= read -r p; do
    p=${p%%[:#]*}
    case $p in *…*) continue ;; esac # an abbreviation, not a path
    [ -e "${p%%/*}" ] || continue # first segment isn't in the repo: a runtime path, a name, a URL
    [ -e "$p" ] || red "$f: points at '$p', which no longer exists"
  done < <(grep -oE '`[^` <>*]+/[^` <>*]*`' "$f" | tr -d '`' | sort -u)
done

[ $due = 1 ] && echo "gc due: tell the user and offer a scoped gc per docs/gc.md — never inside a feature diff"
if [ $fail != 0 ]; then
  echo "check-docs: red"
  exit 1
fi
echo "check-docs: green"
