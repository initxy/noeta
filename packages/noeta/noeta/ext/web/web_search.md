Searches the web and returns the ranked hits as a Markdown list.

- Issues `query` against a web search backend; each hit is a numbered `[title](url)` line plus a short snippet. Optional `count` (default 5, max 20) caps how many hits return.
- Use it for fresh or unknown information; follow up on a promising hit with `WebFetch` to read the page itself.
