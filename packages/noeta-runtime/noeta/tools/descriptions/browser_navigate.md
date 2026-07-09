Navigates the sandbox browser to a URL and returns a snapshot of the loaded page — a numbered list of the interactive elements (links, buttons, inputs) you can act on. The browser keeps its state — tabs, cookies, current page — across calls, so this opens or replaces the page you then read and act on.

Use it to open a web page before extracting or interacting with it; follow up with `browser_extract` to read the page's text or to get a fresh element list after the page changes.
