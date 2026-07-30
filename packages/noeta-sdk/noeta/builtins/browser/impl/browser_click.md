Clicks the interactive element at `index` in the sandbox browser and returns the result of the click.

The `index` is the number of the element (e.g. `[7]`) from the numbered list a prior `browser_navigate` or `browser_extract` returned — read the page first, then click by index. Call `browser_extract` afterward to see the page the click produced; the earlier indices go stale once the page changes.
