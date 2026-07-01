Applies a batch of up to 16 file edits atomically — all succeed or none do.

- Each edit is `replace` (swap a unique substring in an existing file) or `create` (write a brand-new file). All are validated in memory first; an apply-phase failure rolls the whole batch back.
- Paths are workspace-relative; absolute paths and symlink escapes are rejected. A `replace` target's `old` must occur exactly once — include enough surrounding context to make it unique, and copy it verbatim (indentation, trailing spaces, and newlines must match). A `create` target must not exist yet under an existing parent.
- Several `replace` edits may target the **same file** to change multiple places in one call; they apply in array order, and each `old` is matched against the file *as left by the earlier edits in this same call* (so don't include text a previous edit already removed). A `create` cannot be combined with any other edit on the same path — a create writes the whole file, so put its final content in one create. Two different spellings of one path that differ only in case or unicode form are rejected.
- `old`/`new` and `content` each cap at 64 KB (the same safety ceiling as `write`); an absurdly large whole call is rejected — split it into separate calls.
- Leave `before_sha256` unset. It is an optional staleness guard; if set it must be the SHA-256 of the **entire current file** from a recent read — an empty value is ignored, a wrong one fails the edit.
- For a single change to one file, prefer `edit`.
