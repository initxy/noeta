"""Agent prompt text, externalized as standalone Markdown resources.

Each built-in agent's system prompt moves out of the Python string constants in
``noeta.presets`` into a ``<name>.md`` file in this directory, so prompts can be
iterated like docs (clean ``git diff``, editable by non-engineers), matching how
Claude Code writes subagent definitions as standalone files. Structured metadata
(``description`` / ``tools`` / ``capabilities``) stays on
:class:`noeta.client.options.AgentDefinition`: only the large prompt text is
externalized; the one-line roster ``description`` is not.

Files are read by :func:`noeta.protocols.resources.load_markdown` with
``strip=False``: the content is byte-for-byte equal to the original constant
(trailing newline included), so ``AgentSpec`` identity is unchanged by the
externalization.
"""
