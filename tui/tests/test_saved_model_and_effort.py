"""``/model`` and ``/effort`` save their choice for new sessions, as Claude
Code's do: ``max`` lasts the session, ``/effort auto`` forgets the saved
level, the effort follows a model switched to, ``/clear`` keeps both, and
a server's front end (no saving) says the choice lasts the session."""

from __future__ import annotations

import uuid
from pathlib import Path

from noeta.core import models
from noeta.testing import ScriptedProvider, reply

from support import MODEL, make_app, plain, ready, settle, shown, type_line


class Saved:
    """What the ``noeta`` command saves: a model, and an effort per model."""

    def __init__(self, efforts: dict[str, str] | None = None) -> None:
        self.model: str | None = None
        self.efforts = dict(efforts or {})

    def save_model(self, model: str) -> None:
        self.model = model

    def save_effort(self, model: str, level: str | None) -> None:
        if level is None:
            self.efforts.pop(model, None)
        else:
            self.efforts[model] = level

    def effort_for(self, model: str) -> str | None:
        return self.efforts.get(model)

    def start(self) -> dict[str, object]:
        return {"save_model": self.save_model, "save_effort": self.save_effort,
                "effort_for": self.effort_for}


def other_model() -> str:
    name = f"relay-{uuid.uuid4().hex[:8]}"
    models.register_models({name: models.ModelSpec(
        real_model_id=name, context_window=200_000, max_output_tokens=32_000)})
    return name


async def test_effort_levels_are_saved_but_max_lasts_the_session(tmp_path: Path) -> None:
    saved = Saved()
    provider = ScriptedProvider([reply("one"), reply("two"), reply("three")])
    app = make_app(tmp_path, provider, **saved.start())
    async with app.run_test(size=(120, 36)) as pilot:
        await settle(pilot, lambda: ready(app))
        await type_line(pilot, "/effort high")
        await settle(pilot, lambda: "effort set to high from the next request on (saved as "
                                    "your default for new sessions)" in shown(app))
        assert saved.efforts == {MODEL: "high"}
        await type_line(pilot, "go")
        await settle(pilot, lambda: not app.busy and len(provider.requests) == 1)
        await type_line(pilot, "/effort max")
        await settle(pilot, lambda: "effort set to max from the next request on "
                                    "(this session only)" in shown(app))
        assert saved.efforts == {MODEL: "high"}
        await type_line(pilot, "/effort auto")
        await settle(pilot, lambda: "effort set to auto: medium, the model's default"
                                    in shown(app))
        assert saved.efforts == {}
        await type_line(pilot, "again")
        await settle(pilot, lambda: not app.busy and len(provider.requests) == 2)
        assert [r.effort for r in provider.requests] == ["high", None]
        assert f"{MODEL} ◐ medium" in plain(app.query_one("#status"))


async def test_a_model_picked_is_saved_and_its_saved_effort_follows(tmp_path: Path) -> None:
    other = other_model()
    saved = Saved({other: "low"})
    provider = ScriptedProvider([reply("one"), reply("two"), reply("three")])
    app = make_app(tmp_path, provider, **saved.start())
    async with app.run_test(size=(120, 36)) as pilot:
        await settle(pilot, lambda: ready(app))
        await type_line(pilot, "/effort max")
        await type_line(pilot, "go")
        await settle(pilot, lambda: not app.busy and len(provider.requests) == 1)
        await type_line(pilot, f"/model {other}")
        await settle(pilot, lambda: f"model set to {other} from the next request on (saved as "
                                    "your default for new sessions); effort low" in shown(app))
        assert saved.model == other
        await type_line(pilot, "again")
        await settle(pilot, lambda: not app.busy and len(provider.requests) == 2)
        # /clear's new task starts with this session's choices.
        await type_line(pilot, "/clear")
        await settle(pilot, lambda: not app.task_id)
        await type_line(pilot, "anew")
        await settle(pilot, lambda: not app.busy and len(provider.requests) == 3)
        assert [(r.model, r.effort) for r in provider.requests] == [
            (MODEL, "max"), (other, "low"), (other, "low")]
        assert f"{other} ○ low" in plain(app.query_one("#status"))


async def test_a_servers_front_end_keeps_choices_for_the_session(tmp_path: Path) -> None:
    app = make_app(tmp_path, ScriptedProvider([]))
    async with app.run_test(size=(120, 36)) as pilot:
        await settle(pilot, lambda: ready(app))
        await type_line(pilot, "/effort low")
        await settle(pilot, lambda: "effort set to low from the next request on "
                                    "(this session only)" in shown(app))
        await type_line(pilot, f"/model {MODEL}")
        await settle(pilot, lambda: f"model set to {MODEL} from the next request on "
                                    "(this session only)" in shown(app))
        assert app.task_config["effort"] == "low"  # a model switch keeps it


async def test_a_choice_that_cannot_be_saved_says_why(tmp_path: Path) -> None:
    def refuse(model: str, level: str | None) -> None:
        raise ValueError("settings.json is not a JSON object")

    app = make_app(tmp_path, ScriptedProvider([]), save_effort=refuse)
    async with app.run_test(size=(120, 36)) as pilot:
        await settle(pilot, lambda: ready(app))
        await type_line(pilot, "/effort xhigh")
        await settle(pilot, lambda: "effort set to xhigh from the next request on (not saved: "
                                    "settings.json is not a JSON object)" in shown(app))
        assert app.effort == "xhigh"
