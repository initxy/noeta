// Shared prism code highlighter (B18).
//
// Used by BOTH the workspace file preview and assistant markdown fenced code
// blocks. Prism emits only `class="token <type>"` (an EMPTY theme), so all
// colors come from the `.code-hl .token.*` rules in app.css — dark/light follow
// the theme automatically and no third-party prism theme bloats the bundle. The
// container always carries `code-hl` (the token-color surface) plus any caller
// class.
//
// The language gate + size gate live in ./prism-languages.js (pure, unit-tested):
// callers resolve a SUPPORTED language via `prismLanguage()` / `languageForPath()`
// before rendering, so `<Highlight>` is never handed an unbundled grammar.

import { Highlight } from "prism-react-renderer";

const PRISM_EMPTY_THEME = { plain: {}, styles: [] };

function HighlightedCode({ code, language, className }) {
  return (
    <Highlight code={code} language={language} theme={PRISM_EMPTY_THEME}>
      {({ tokens, getLineProps, getTokenProps }) => (
        <pre className={className ? `${className} code-hl` : "code-hl"}>
          {tokens.map((line, i) => (
            <div key={i} {...getLineProps({ line })}>
              {line.map((token, k) => (
                <span key={k} {...getTokenProps({ token })} />
              ))}
            </div>
          ))}
        </pre>
      )}
    </Highlight>
  );
}

export { HighlightedCode, PRISM_EMPTY_THEME };
