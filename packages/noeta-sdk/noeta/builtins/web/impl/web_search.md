Runs a web search and returns the top ranked hits rendered to Markdown.

- Issues `query` against a web search backend and returns `{query, count, content, content_ref, truncated}`, where `content` is a numbered Markdown list of hits (each a `[title](url)` line plus a short snippet). The full rendering is offloaded as an artifact; when inline content would exceed the byte budget you get an excerpt plus the ref and `truncated: true`. Optional `count` (default 5, max 20) caps how many hits to return.
- Use it to discover URLs and current information you do not already have — finding pages, checking recent facts, locating documentation or sources on the open web.
- It does not read a specific page you already have: to fetch a known URL's content use `webfetch`; to read a workspace file use `read`. It searches the public web, not the workspace.
- `query` must be a non-empty string. The tool is only available when a search backend key is configured; if it is absent here, the capability is not enabled for this session.
