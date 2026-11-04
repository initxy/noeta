import ReactMarkdown, { defaultUrlTransform, type Components } from "react-markdown";
import rehypeHighlight from "rehype-highlight";
import remarkGfm from "remark-gfm";

const LOCAL_IMAGE = /^(data:image\/|blob:)/i;

/** Links open in a new tab; a remote image is not fetched (it would tell its
 * server the page was read) but shown as a link to it. */
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
