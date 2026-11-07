import { codeblockSlot, Slot } from "@noeta-ai/web-sdk";
import type { Element, ElementContent } from "hast";
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

/** GitHub-flavored markdown with highlighted code, as the model writes it. */
export function Markdown({ text }: { text: string }) {
  return (
    <div className="md">
      <ReactMarkdown
        remarkPlugins={[remarkGfm]}
        rehypePlugins={[[rehypeHighlight, { detect: true, ignoreMissing: true }]]}
        components={components}
        urlTransform={(url, key) => (key === "src" && LOCAL_IMAGE.test(url) ? url : defaultUrlTransform(url))}
      >
        {text}
      </ReactMarkdown>
    </div>
  );
}
