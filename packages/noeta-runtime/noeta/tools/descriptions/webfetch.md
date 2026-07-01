Fetches a public web page over HTTP(S) and returns its content rendered to Markdown.

- Performs a GET on `url`, converts the HTML to compact Markdown (headings, links, list items; scripts, styles, and `<head>` stripped), and returns `{url, title, content, content_ref, truncated}`. The full rendering is offloaded as an artifact; when inline content would exceed the byte budget you get an excerpt plus the ref and `truncated: true`.
- `url` must be a well-formed `http://` or `https://` URL.
- The page must be publicly reachable: webfetch sends no credentials, so authenticated / logged-in / intranet URLs fail (e.g. 401/403). Use an authenticated tool for those.
- It fetches a URL you already have; it does not search. To read a workspace file, use `read`.
