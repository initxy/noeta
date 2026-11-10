import { codeblockSlot, Slot } from "@initxy/noeta-web-sdk";
import type { Element, ElementContent } from "hast";
import { memo } from "react";
import ReactMarkdown, { defaultUrlTransform, type Components } from "react-markdown";
import rehypeHighlight from "rehype-highlight";
import remarkGfm from "remark-gfm";

const LOCAL_IMAGE = /^(data:image\/|blob:)/i;

function textOf(nodes: ElementContent[]): string {
  return nodes.map((n) => (n.type === "text" ? n.value : n.type === "element" ? textOf(n.children) : "")).join("");
}

/** A fenced block's language and source, from its `<pre><code class="language-…">`. */
function fenced(pre: Element | undefined): { language: string; code: string } | null {
  const code = pre?.children.find((c): c is Element => c.type === "element" && c.tagName === "code");
  const classes = code?.properties?.className;
  const language = (Array.isArray(classes) ? classes : []).map(String).find((c) => c.startsWith("language-"))?.slice(9);
  return code && language ? { language, code: textOf(code.children).replace(/\n$/, "") } : null;
}

/** Links open in a new tab; a remote image is not fetched (it would tell its
 * server the page was read) but shown as a link to it; a fenced block is
 * the `codeblock:<language>` slot. */
const components: Components = {
  a: ({ node: _node, ...props }) => <a {...props} target="_blank" rel="noopener noreferrer" />,
  img: ({ node: _node, src, alt }) => {
    const url = typeof src === "string" ? src : "";
    if (LOCAL_IMAGE.test(url)) return <img src={url} alt={alt ?? ""} />;
    return (
      <a href={url} target="_blank" rel="noopener noreferrer" className="remote-image" title={url}>
        Image: {alt || url}
      </a>
    );
  },
  pre: ({ node, ...props }) => {
    const block = fenced(node);
    const own = <pre {...props} />;
    return block ? (
      <Slot name={codeblockSlot(block.language)} props={block}>
        {own}
      </Slot>
    ) : (
      own
    );
  },
};

const FENCE = /^ {0,3}(`{3,}|~{3,})/;

/** A streaming text cut into its finished blocks and the one still being
 * written: at a blank line outside a fenced block, before a line that starts
 * a block of its own (not indented, not a list item, which may continue a
 * list). `open`: the last block is a fenced block not closed yet. */
export function streamingBlocks(text: string): { blocks: string[]; open: boolean } {
  const blocks: string[] = [];
  let current: string[] = [];
  let fence: string | null = null;
  let blank = false;
  for (const line of text.split("\n")) {
    if (fence !== null) {
      current.push(line);
      const close = FENCE.exec(line);
      if (close && close[1][0] === fence[0] && close[1].length >= fence.length && !line.slice(close[0].length).trim()) fence = null;
      continue;
    }
    if (!line.trim()) {
      current.push(line);
      blank = true;
      continue;
    }
    if (blank && !/^\s/.test(line) && !/^([-*+]|\d+[.)])\s/.test(line)) {
      blocks.push(current.join("\n"));
      current = [];
    }
    blank = false;
    current.push(line);
    const open = FENCE.exec(line);
    if (open) fence = open[1];
  }
  blocks.push(current.join("\n"));
  return { blocks, open: fence !== null };
}

const REMARK = [remarkGfm];
// Only a block that names its language is highlighted: guessing one costs
// more than drawing the rest of the message. The plugin makes its own
// highlighter (every grammar registered) each time a processor is made,
// which react-markdown does on every render: one is made here, the first time code is drawn.
let highlighter: ReturnType<typeof rehypeHighlight> | undefined;
const HIGHLIGHT: NonNullable<Parameters<typeof ReactMarkdown>[0]["rehypePlugins"]> = [() => (highlighter ??= rehypeHighlight())];
const PLAIN: typeof HIGHLIGHT = [];
const hasCode = (text: string) => text.includes("```") || text.includes("~~~");
const urlTransform = (url: string, key: string) => (key === "src" && LOCAL_IMAGE.test(url) ? url : defaultUrlTransform(url));

const Parsed = memo(function Parsed({ text, highlight }: { text: string; highlight: boolean }) {
  return (
    <ReactMarkdown remarkPlugins={REMARK} rehypePlugins={highlight && hasCode(text) ? HIGHLIGHT : PLAIN} components={components} urlTransform={urlTransform}>
      {text}
    </ReactMarkdown>
  );
});

/** GitHub-flavored markdown with highlighted code, as the model writes it;
 * parsed again only when the text changes. `streaming`: text still being
 * written, of which only the block being written is parsed again, and a
 * code block is highlighted once it is closed. */
export const Markdown = memo(function Markdown({ text, streaming }: { text: string; streaming?: boolean }) {
  if (!streaming) {
    return (
      <div className="md">
        <Parsed text={text} highlight />
      </div>
    );
  }
  const { blocks, open } = streamingBlocks(text);
  return (
    <div className="md">
      {blocks.map((block, i) => (
        <Parsed key={i} text={block} highlight={!open || i < blocks.length - 1} />
      ))}
    </div>
  );
});
