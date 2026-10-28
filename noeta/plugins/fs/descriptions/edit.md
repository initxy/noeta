Replaces an exact string in a file.

- Read the file in this conversation before editing it; otherwise the edit fails.
- `old_string` must match the file exactly, indentation included, and occur only once; otherwise the edit fails. Add surrounding lines to make it unique, or set `replace_all`.
- Leave out Read's line prefix (the number and the tab after it) when you copy text into `old_string` or `new_string`.
- `replace_all: true` replaces every occurrence, for example to rename something throughout the file.
