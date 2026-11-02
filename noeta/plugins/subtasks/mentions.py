"""``@agent-<type>`` in the person's prompt (Claude Code's agent mention, and
its ``@"<type> (agent)"`` form): an agent type this task can start is
named to the model as the person's wish to have it do the work."""

from __future__ import annotations

import re
from collections.abc import Collection, Sequence

from noeta.core.extension import Reminder, TaskView
from noeta.core.types import ToolResult

__all__ = ["AGENT_MENTION", "agent_mentions", "mention_reminders"]

#: Where a mention may start: the prompt's start, a space or CJK punctuation.
#: (the ideographic full stop and comma, the full-width ? and !).
_CJK = "".join(map(chr, (0x3002, 0x3001, 0xFF1F, 0xFF01)))
_BEFORE = r"(?:^|(?<=[\s" + _CJK + r"]))"
_QUOTED = re.compile(_BEFORE + r'@"([\w:.@-]+) \(agent\)"')
_PLAIN = re.compile(_BEFORE + r"@agent-([\w:.@-]+)")

AGENT_MENTION = (
    'The user asks for the "{type}" agent: start it with the Agent tool and give it the '
    "context it needs.")


def agent_mentions(text: str) -> list[str]:
    """The agent types ``text`` mentions, in order, each once."""
    found = [m.group(1) for m in _QUOTED.finditer(text)]
    found += [m.group(1) for m in _PLAIN.finditer(text)]
    return list(dict.fromkeys(found))


def mention_reminders(view: TaskView, types: Collection[str]) -> Sequence[Reminder]:
    """A note per agent type of ``types`` the prompts after the model's last
    reply mention (with the first model call that sees them)."""
    prompts: list[str] = []
    for m in reversed(view.state.messages):
        if m.role == "assistant":
            break
        if m.from_person and m.origin == "human" \
                and not any(isinstance(b, ToolResult) for b in m.content):
            prompts.insert(0, m.text)
    wanted = [t for text in prompts for t in agent_mentions(text) if t in types]
    return [Reminder(text=AGENT_MENTION.format(type=t), kind="agent_mention", data={"type": t})
            for t in dict.fromkeys(wanted)]
