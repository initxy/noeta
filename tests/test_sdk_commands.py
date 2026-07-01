"""Slash-command mechanism lives in the SDK.

Three buckets:

1. Mechanism module is content-free: ``noeta.execution.commands`` does not
   mention any concrete command or agent name, and has no module-level catalog
   mapping.
2. Generic helpers work with a caller-supplied catalog (one prompt + one local
   command we make up here); every public API is exercised.
3. Product compatibility: the public API surface of ``noeta.agent.commands`` is
   unchanged, the built-in catalog still behaves exactly as before, and the
   SlashCommand dataclass is re-exported from the SDK module (proving it is
   the *same* type, not a product-local copy).
"""

from __future__ import annotations

import ast
import inspect

import pytest

import noeta.execution.commands as sdk_commands
from noeta.execution.commands import (
    CommandResolution,
    SlashCommand,
    first_sentence,
    get_command,
    list_commands,
    render_help,
    resolve_command,
)

# ---------------------------------------------------------------------------
# 1. Mechanism is content-free
# ---------------------------------------------------------------------------

# Concrete command/agent names that belong to the product, not the SDK.
_FORBIDDEN_LITERALS = (
    "review",
    "verify",
    "general-purpose",
)


def test_mechanism_module_source_has_no_product_specific_literals() -> None:
    """The SDK mechanism module must not bake in any noeta-agent command name.

    We check the raw source (plus the parsed AST for string constants) so a
    docstring example doesn't accidentally regress the "mechanism vs. content"
    boundary.
    """

    source = inspect.getsource(sdk_commands)
    tree = ast.parse(source)

    string_constants: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            string_constants.append(node.value)

    for literal in _FORBIDDEN_LITERALS:
        # The module-level docstring uses the example "review" when describing
        # the SlashCommand.name field ("e.g. ``'review'``"). That's still a
        # concrete product literal — if it snuck back in, fail loudly.
        for s in string_constants:
            assert literal not in s, (
                f"Forbidden product literal {literal!r} found in SDK "
                f"commands source (constant: {s!r})"
            )


def test_mechanism_module_has_no_module_level_command_catalog() -> None:
    """No module-level dict constant values of SlashCommand.

    The content layer (BUILTIN_COMMANDS) must live in the product only; the SDK
    mechanism takes the catalog as an argument instead.
    """

    source = inspect.getsource(sdk_commands)
    tree = ast.parse(source)

    # Look for module-level assignments where the RHS is a Dict or DictComp.
    dict_constant_names: list[str] = []
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    if isinstance(node.value, (ast.Dict, ast.DictComp)):
                        dict_constant_names.append(target.id)

    # If any such dict constant exists, walk it and make sure no value is a
    # SlashCommand construction.  (Nothing in the current module should be a
    # SlashCommand catalog, so this is belt-and-suspenders.)
    source_lines = source.splitlines()
    for name in dict_constant_names:
        for line in source_lines:
            # Rough but sufficient: any line containing both the dict name and
            # a SlashCommand(...) construction is a red flag.
            if name in line and "SlashCommand(" in line:
                pytest.fail(
                    f"Found a module-level dict {name!r} whose entries look "
                    f"like SlashCommand values in the SDK mechanism module. "
                    f"Move the catalog to the product layer."
                )


# ---------------------------------------------------------------------------
# 2. Generic helpers with a caller-supplied catalog
# ---------------------------------------------------------------------------

# An entirely made-up catalog so we prove the SDK doesn't depend on the
# noeta-agent product catalog.
_FAKE_PROMPT = SlashCommand(
    name="greet",
    description="Greet the day.",
    kind="prompt",
    skill="greeter",
    agent="hello-agent",
    argument_hint="[subject]",
)
_FAKE_LOCAL = SlashCommand(
    name="banner",
    description="Show the banner.",
    kind="local",
)
_FAKE_COMMANDS: dict[str, SlashCommand] = {
    _FAKE_PROMPT.name: _FAKE_PROMPT,
    _FAKE_LOCAL.name: _FAKE_LOCAL,
}


def test_list_commands_sorts_by_name() -> None:
    names = [c.name for c in list_commands(_FAKE_COMMANDS)]
    assert names == sorted(names)
    assert names == ["banner", "greet"]


def test_get_command_returns_matching_entry() -> None:
    assert get_command("greet", commands=_FAKE_COMMANDS) is _FAKE_PROMPT
    assert get_command("banner", commands=_FAKE_COMMANDS) is _FAKE_LOCAL


def test_get_command_unknown_raises_keyerror_with_sorted_available_list() -> None:
    with pytest.raises(KeyError) as info:
        get_command("nope", commands=_FAKE_COMMANDS)

    message = str(info.value)
    # Exact shape: "unknown command 'nope'; available: banner, greet"
    assert "unknown command 'nope'" in message
    assert "available: banner, greet" in message


def test_render_help_one_line_per_command_sorted() -> None:
    rendered = render_help(_FAKE_COMMANDS)
    lines = rendered.splitlines()
    assert len(lines) == 2
    assert lines[0] == "/banner — Show the banner."
    assert lines[1] == "/greet — Greet the day."


def test_resolve_prompt_command_passes_agent_skill_arguments() -> None:
    res = resolve_command(
        "greet",
        "the world",
        commands=_FAKE_COMMANDS,
        local_renderers={},
    )
    assert isinstance(res, CommandResolution)
    assert res.command is _FAKE_PROMPT
    assert res.agent == "hello-agent"
    assert res.skill == "greeter"
    assert res.arguments == "the world"
    assert res.text is None


def test_resolve_prompt_command_default_arguments_empty() -> None:
    res = resolve_command("greet", commands=_FAKE_COMMANDS, local_renderers={})
    assert res.arguments == ""


def test_resolve_local_command_uses_renderer() -> None:
    def _render() -> str:
        return "*** BANNER ***"

    res = resolve_command(
        "banner",
        "ignored args",
        commands=_FAKE_COMMANDS,
        local_renderers={"banner": _render},
    )
    assert res.command is _FAKE_LOCAL
    assert res.agent is None
    assert res.skill is None
    # Local commands never carry user arguments out.
    assert res.arguments == ""
    assert res.text == "*** BANNER ***"


def test_resolve_local_command_without_renderer_yields_empty_text() -> None:
    res = resolve_command("banner", commands=_FAKE_COMMANDS, local_renderers={})
    assert res.text == ""
    assert res.agent is None


def test_resolve_unknown_command_raises_keyerror() -> None:
    with pytest.raises(KeyError):
        resolve_command("missing", commands=_FAKE_COMMANDS, local_renderers={})


def test_first_sentence_strips_and_truncates_at_first_period() -> None:
    assert first_sentence("Hello. World.") == "Hello."
    assert first_sentence("\n\nOne line. Follow up.") == "One line."
    # No period -> return the whole first (stripped) line.
    assert first_sentence("no period here\nmore") == "no period here"
    assert first_sentence("") == ""
    assert first_sentence("   ") == ""


# ---------------------------------------------------------------------------
# 3. Product compatibility — public API unchanged
# ---------------------------------------------------------------------------

# Import from the PRODUCT public module (the surface callers actually use).
from noeta.agent.commands import (  # noqa: E402
    BUILTIN_COMMANDS,
    resolve_command as product_resolve,
)


def test_product_builtin_commands_values_are_sdk_slashcommand() -> None:
    """Each BUILTIN_COMMANDS value uses the *SDK's* SlashCommand type.

    If someone reintroduces a product-local SlashCommand copy the class's
    __module__ will be ``noeta.agent.commands.registry`` (or similar) instead of
    ``noeta.execution.commands`` — the whole point of issue 04 is that the type
    is defined once, in the SDK.
    """

    assert BUILTIN_COMMANDS, "BUILTIN_COMMANDS must be non-empty"
    for name, value in BUILTIN_COMMANDS.items():
        assert isinstance(value, SlashCommand), f"{name}: not a SlashCommand"
        # Dataclass instance -> dataclass type's __module__.
        assert type(value).__module__ == "noeta.execution.commands", (
            f"BUILTIN_COMMANDS[{name!r}] is not the SDK SlashCommand. "
            f"Got type from {type(value).__module__!r}."
        )


def test_product_reexported_slashcommand_is_sdk_slashcommand() -> None:
    # ``from noeta.agent.commands import SlashCommand`` must resolve to the
    # *same* class object we have in the SDK module.
    from noeta.agent.commands import SlashCommand as ProductSlashCommand

    assert ProductSlashCommand is SlashCommand


def test_product_resolve_review_still_targets_general_purpose() -> None:
    # The change (review → general-purpose) must be preserved.
    res = product_resolve("review")
    assert res.agent == "general-purpose"
    assert res.skill == "review"
    assert res.text is None


def test_product_resolve_help_mentions_every_expected_command() -> None:
    res = product_resolve("help")
    assert res.text is not None
    # A sample of the expected built-in commands must appear as "/name".
    for name in ("review", "handoff"):
        assert f"/{name}" in res.text, (
            f"'/{name}' missing from /help text — the renderer may be broken."
        )
