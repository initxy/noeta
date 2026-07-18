"""``noeta.agent`` built-in skill pack discovery (D8).

The built-in coding skills (commit/review/init/handoff/verify/simplify)
are PRODUCT content — they ship inside the noeta-agent namespace at
``noeta/agent/skills_builtin/<name>/SKILL.md``. The skill *machine*
(indexer/renderer/activation) lives in the SDK (``noeta.context.skills``
+ ``noeta.execution.skills``); this module only anchors the product's
bundled pack on disk.

``Path(__file__)`` anchoring (not ``importlib.resources``) is deliberate:
``noeta.agent`` is a PEP 420 namespace package and
``importlib.resources.files()`` only gained namespace-package support in
Python 3.12, while this project supports 3.11+. The same pattern the old
``noeta.execution.skills.BUILTIN_SKILLS_DIR`` used.
"""

from __future__ import annotations

from pathlib import Path

from noeta.context.skills import SkillIndexer, SkillRegistry


__all__ = [
    "BUILTIN_SKILLS_DIR",
    "DEFAULT_GLOBAL_SKILLS_DIR",
    "load_builtin_skills",
]


#: Package-bundled built-in skills live at ``noeta/agent/skills_builtin``.
#: ``__file__`` is ``noeta/agent/skills.py``; ``parent`` is the namespace dir.
BUILTIN_SKILLS_DIR: Path = Path(__file__).resolve().parent / "_skills_builtin"

#: The global (cross-workspace) skill pack, the middle tier
#: between the built-in pack and a workspace-local ``.noeta/skills``. The agent
#: layer wires it as a deployment dir so skills do NOT follow the per-session
#: workspace; ``~`` resolves against the running user's home.
DEFAULT_GLOBAL_SKILLS_DIR: Path = Path("~/.noeta/skills").expanduser()


def load_builtin_skills() -> SkillRegistry:
    """Index the package-bundled built-in skills (D2).

    Built-in skills ship inside ``noeta/agent/skills_builtin/<name>/SKILL.md``
    and are loaded independently of the per-workspace skill pack so the
    default runner / replay path is untouched. A missing directory yields
    an **empty** Registry (same forgiving semantics as the indexer).
    """
    return SkillIndexer(BUILTIN_SKILLS_DIR).index()
