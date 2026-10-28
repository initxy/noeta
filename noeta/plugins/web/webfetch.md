Fetches a URL, turns the page into Markdown and answers `prompt` about it with a small, fast model: you get the answer, not the page.

- Pages behind a login, or otherwise private, fail. For those use an MCP tool that has access, or `gh` for GitHub.
- `localhost` and host names without a dot cannot be fetched; reach a local server with `curl` through Bash.
- `http` URLs are fetched over `https`.
- A redirect to a different host is not followed: the result names the new URL, and you call again with it.
- Pages are cached per URL for 15 minutes, so asking again about the same page is cheap.
- A page too long for one call is answered from the part that fits, and the result gives the `offset` to continue from.
