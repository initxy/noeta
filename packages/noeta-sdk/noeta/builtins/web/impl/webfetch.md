Fetches a public URL and returns its content rendered as Markdown.

- Performs a GET on `url`, converts the HTML to compact Markdown (headings, links, list items; scripts, styles, and `<head>` stripped), and returns a `Title:` / `URL:` header followed by the page body. A pathologically long page is truncated with a notice naming the total size.
- Only public pages: an intranet or logged-in URL answers 401/403 (or is unreachable) and the call fails with the cause named — don't retry those.
- Redirects are followed. To search the web rather than fetch a known page, use `WebSearch`.
