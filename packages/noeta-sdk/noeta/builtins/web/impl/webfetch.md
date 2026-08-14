Fetches a URL, converts the page to Markdown, and answers `prompt` against it — returning the answer, not the raw page.

- Only public pages: an intranet or logged-in URL answers 401/403 (or is unreachable) and the call fails with the cause named — don't retry those.
- HTTP is upgraded to HTTPS. Cross-host redirects are returned to you rather than followed; call again with the redirect URL.
- Fetched pages are cached for 15 minutes per URL, so a follow-up `prompt` about the same page is cheap.
- To search the web rather than fetch a known page, use `WebSearch`.
