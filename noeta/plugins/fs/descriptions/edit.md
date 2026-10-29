Replaces an exact string in a file.

- The file must have been read with Read earlier in this conversation, or the edit fails.
- `old_string` has to match the file character for character, indentation included, and appear only once; otherwise the edit fails. Add surrounding lines to make it unique, or set `replace_all`.
- Leave out Read's line prefix (the number and the tab after it) when you copy text into `old_string` or `new_string`.
- With `replace_all: true` every occurrence is replaced, for example to rename something throughout the file.
