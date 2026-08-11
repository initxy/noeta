"""A plugin contributing one ``turn_intake`` reminder provider.

Written the way a host would write it — every name comes from ``noeta.sdk`` —
so loading it through ``load_plugins(modules=[...])`` exercises the public path
end to end. The provider dispatches through a module-level registry rather than
closing over per-run state: a manifest ``ref`` is a static import path, so
"which text for this run" has to be looked up at call time, not baked in.
"""

from __future__ import annotations

from typing import Callable, Optional

from noeta.sdk import TURN_INTAKE, PluginBuilder, RecallView, Reminder


plugin = PluginBuilder("host-intake")

#: ``agent name -> render()``. A host rebinds this per run.
RENDERERS: dict[str, Callable[[], str]] = {}
#: Every view the provider was handed, for assertions.
SEEN: list[RecallView] = []


def bind(agent: str, render: Callable[[], str]) -> None:
    RENDERERS[agent] = render


def reset() -> None:
    RENDERERS.clear()
    SEEN.clear()


@plugin.reminder_provider(name="live_snapshot", seams=(TURN_INTAKE,))
def live_snapshot(view: RecallView) -> tuple[Reminder, ...]:
    SEEN.append(view)
    render: Optional[Callable[[], str]] = RENDERERS.get("main")
    if render is None:
        return ()
    text = (render() or "").strip()
    return (Reminder(text=text, origin="system"),) if text else ()
