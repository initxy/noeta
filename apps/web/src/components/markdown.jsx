// Minimal, dependency-free Markdown renderer for assistant prose.
//
// Why hand-rolled: the rest of this web surface (the ai-elements, the shared
// reducer) is deliberately written without a UI/markdown framework, and the
// runtime is served headless behind noeta-agent — adding react-markdown +
// remark would pull a non-trivial dependency tree for what assistant output
// actually needs. This covers the common chat subset:
//
//   blocks : fenced code, ATX headings, blockquotes, ordered/unordered lists,
//            GFM-ish tables, thematic breaks, paragraphs
//   inline : `code`, **bold**, *italic*, [text](url), hard line breaks
//
// Everything renders to React nodes (never dangerouslySetInnerHTML), so it is
// XSS-safe by construction; links are restricted to http(s)/mailto/relative.

import { memo, useMemo } from "react";
import { HighlightedCode } from "./HighlightedCode.jsx";
import { prismLanguage, canHighlightSize } from "./prism-languages.js";

const SAFE_HREF = /^(https?:\/\/|mailto:|\/)/i;

// Perf fix #2 — parsing markdown is pure and stable for a given text, but history
// backfill triggers a full-list re-render, and we used to re-parse every bubble each
// time (N re-renders x full re-parse = wasted CPU). useMemo caches the parse result
// (keyed on text); React.memo lets bubbles whose text/className are unchanged skip
// re-rendering when the parent re-renders — together they kill the lag. memo works
// here because renderTurnItem passes <Markdown> only the raw string text (and an
// optional className) — no freshly-built object/callback props each render.
function MarkdownImpl({ text, className }) {
  const blocks = useMemo(
    () => parseBlocks(String(text == null ? "" : text)),
    [text],
  );
  return (
    <div className={className ? `ai-markdown ${className}` : "ai-markdown"}>
      {blocks.map((block, index) => renderBlock(block, index))}
    </div>
  );
}

const Markdown = memo(MarkdownImpl);

// ---------------------------------------------------------------------------
// Block-level parsing
// ---------------------------------------------------------------------------

function isBlockStart(line) {
  return (
    /^\s*(`{3,}|~{3,})/.test(line) ||
    /^\s*#{1,6}\s+/.test(line) ||
    /^\s*>/.test(line) ||
    /^\s*([-*+]|\d+[.)])\s+/.test(line) ||
    /^\s*([-*_])(\s*\1){2,}\s*$/.test(line)
  );
}

function parseBlocks(src) {
  const lines = src.replace(/\r\n?/g, "\n").split("\n");
  const blocks = [];
  let i = 0;

  while (i < lines.length) {
    const line = lines[i];

    if (/^\s*$/.test(line)) {
      i += 1;
      continue;
    }

    // Fenced code block (``` or ~~~), with an optional language hint.
    const fence = line.match(/^\s*(`{3,}|~{3,})(.*)$/);
    if (fence) {
      const marker = fence[1][0];
      const closer = new RegExp(`^\\s*${marker}{3,}\\s*$`);
      const body = [];
      i += 1;
      while (i < lines.length && !closer.test(lines[i])) {
        body.push(lines[i]);
        i += 1;
      }
      i += 1; // consume the closing fence (or EOF)
      blocks.push({ type: "code", lang: fence[2].trim(), text: body.join("\n") });
      continue;
    }

    const heading = line.match(/^\s*(#{1,6})\s+(.*?)\s*#*\s*$/);
    if (heading) {
      blocks.push({ type: "heading", level: heading[1].length, text: heading[2] });
      i += 1;
      continue;
    }

    // Thematic break: a line of 3+ matching -, * or _ (allowing spaces).
    if (/^\s*([-*_])(\s*\1){2,}\s*$/.test(line)) {
      blocks.push({ type: "hr" });
      i += 1;
      continue;
    }

    if (/^\s*>/.test(line)) {
      const body = [];
      while (i < lines.length && /^\s*>/.test(lines[i])) {
        body.push(lines[i].replace(/^\s*>\s?/, ""));
        i += 1;
      }
      blocks.push({ type: "quote", text: body.join("\n") });
      continue;
    }

    // GFM table: a header row followed by a |---|:--:| delimiter row.
    if (
      line.includes("|") &&
      i + 1 < lines.length &&
      /^\s*\|?[\s:|-]*-[\s:|-]*\|?\s*$/.test(lines[i + 1])
    ) {
      const header = splitTableRow(line);
      const rows = [];
      i += 2;
      while (i < lines.length && lines[i].includes("|") && !/^\s*$/.test(lines[i])) {
        rows.push(splitTableRow(lines[i]));
        i += 1;
      }
      blocks.push({ type: "table", header, rows });
      continue;
    }

    if (/^\s*([-*+]|\d+[.)])\s+/.test(line)) {
      const ordered = /^\s*\d+[.)]\s+/.test(line);
      const items = [];
      while (i < lines.length && /^\s*([-*+]|\d+[.)])\s+/.test(lines[i])) {
        let item = lines[i].replace(/^\s*([-*+]|\d+[.)])\s+/, "");
        i += 1;
        // Fold indented continuation lines into the current item.
        while (
          i < lines.length &&
          !/^\s*$/.test(lines[i]) &&
          /^\s+/.test(lines[i]) &&
          !/^\s*([-*+]|\d+[.)])\s+/.test(lines[i])
        ) {
          item += `\n${lines[i].trim()}`;
          i += 1;
        }
        items.push(item);
      }
      blocks.push({ type: "list", ordered, items });
      continue;
    }

    // Paragraph: gather consecutive lines until a blank line or a new block.
    const para = [line];
    i += 1;
    while (i < lines.length && !/^\s*$/.test(lines[i]) && !isBlockStart(lines[i])) {
      para.push(lines[i]);
      i += 1;
    }
    blocks.push({ type: "para", text: para.join("\n") });
  }

  return blocks;
}

function splitTableRow(line) {
  return line
    .trim()
    .replace(/^\|/, "")
    .replace(/\|$/, "")
    .split("|")
    .map((cell) => cell.trim());
}

// ---------------------------------------------------------------------------
// Block rendering
// ---------------------------------------------------------------------------

function renderBlock(block, key) {
  switch (block.type) {
    case "code": {
      // B18 — syntax-highlight via the shared prism component when the fence
      // language is recognized and the block is within the perf gate; otherwise
      // fall back to plain text (prism is never handed an unsupported grammar).
      const lang = prismLanguage(block.lang);
      if (lang && canHighlightSize(block.text)) {
        return (
          <HighlightedCode
            key={key}
            code={block.text}
            language={lang}
            className="ai-code-block"
          />
        );
      }
      return (
        <pre className="ai-code-block" key={key}>
          <code>{block.text}</code>
        </pre>
      );
    }
    case "heading": {
      const Tag = `h${Math.min(block.level, 6)}`;
      return <Tag key={key}>{renderInline(block.text, `h${key}`)}</Tag>;
    }
    case "hr":
      return <hr key={key} />;
    case "quote":
      return (
        <blockquote key={key}>
          {parseBlocks(block.text).map((inner, index) => renderBlock(inner, `${key}-${index}`))}
        </blockquote>
      );
    case "list": {
      const Tag = block.ordered ? "ol" : "ul";
      return (
        <Tag key={key}>
          {block.items.map((item, index) => (
            <li key={index}>{renderInline(item, `${key}-${index}`)}</li>
          ))}
        </Tag>
      );
    }
    case "table":
      return (
        <table className="ai-md-table" key={key}>
          <thead>
            <tr>
              {block.header.map((cell, index) => (
                <th key={index}>{renderInline(cell, `${key}-th${index}`)}</th>
              ))}
            </tr>
          </thead>
          <tbody>
            {block.rows.map((row, rowIndex) => (
              <tr key={rowIndex}>
                {row.map((cell, cellIndex) => (
                  <td key={cellIndex}>{renderInline(cell, `${key}-${rowIndex}-${cellIndex}`)}</td>
                ))}
              </tr>
            ))}
          </tbody>
        </table>
      );
    default:
      return <p key={key}>{renderInline(block.text, `p${key}`)}</p>;
  }
}

// ---------------------------------------------------------------------------
// Inline rendering
// ---------------------------------------------------------------------------

// Single left-to-right pass over the inline grammar: at each step the earliest
// of code / link / bold / italic wins. Code is terminal (its body is literal —
// never parsed for emphasis), while bold/italic recurse so a `code` span *inside*
// **bold** still renders. (The old two-stage version split code spans out first,
// which stranded the **…** delimiters on either side of an inner code span and
// left them as literal text — the "**`pkg`** keeps its asterisks" bug.)
//
// Group map: 1 code · 2 link-text 3 link-href · 4 bold(**) 5 bold(__) ·
//            6 italic(*) 7 italic(_)
const INLINE_RE =
  /`([^`]+)`|\[([^\]]+)\]\(([^)\s]+)(?:\s+"[^"]*")?\)|\*\*([^*]+?)\*\*|__([^_]+?)__|\*([^*\n]+?)\*|_([^_\n]+?)_/;

function renderInline(text, keyBase) {
  const out = [];
  let rest = String(text == null ? "" : text);
  let k = 0;
  while (rest) {
    const match = INLINE_RE.exec(rest);
    if (!match) {
      pushText(out, rest, `${keyBase}-t${k++}`);
      break;
    }
    if (match.index > 0) {
      pushText(out, rest.slice(0, match.index), `${keyBase}-t${k++}`);
    }
    if (match[1] != null) {
      out.push(
        <code className="ai-code-inline" key={`${keyBase}-c${k++}`}>
          {match[1]}
        </code>,
      );
    } else if (match[2] != null) {
      const href = SAFE_HREF.test(match[3]) ? match[3] : "#";
      out.push(
        <a href={href} target="_blank" rel="noopener noreferrer" key={`${keyBase}-a${k++}`}>
          {match[2]}
        </a>,
      );
    } else if (match[4] != null || match[5] != null) {
      const inner = match[4] != null ? match[4] : match[5];
      out.push(
        <strong key={`${keyBase}-b${k++}`}>{renderInline(inner, `${keyBase}-b${k}`)}</strong>,
      );
    } else {
      const inner = match[6] != null ? match[6] : match[7];
      out.push(<em key={`${keyBase}-i${k++}`}>{renderInline(inner, `${keyBase}-i${k}`)}</em>);
    }
    rest = rest.slice(match.index + match[0].length);
  }
  return out;
}

// Plain text, with single newlines becoming hard line breaks.
function pushText(out, text, keyBase) {
  const parts = text.split("\n");
  parts.forEach((part, index) => {
    if (index > 0) out.push(<br key={`${keyBase}-br${index}`} />);
    if (part) out.push(part);
  });
}

export { Markdown };
