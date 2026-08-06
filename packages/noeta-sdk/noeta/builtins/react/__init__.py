"""``react`` — the default decision-mapping policy built-in.

The default policy's identity is pinned SDK-side as ``POLICY_REF ("react",
"1")`` for every compiled agent, so this manifest declares only what is a
genuine per-agent contribution: the two workflow control tools, plus the
compaction escape hatch (the ``RecallHistory`` control tool and its
``collapsed-context`` reminder — compaction is this policy's mechanism, and
declaring both here keeps tool and pointer present or absent together). That
makes ``react`` replaceable but not removable — ``disabled_builtins=["react"]``
raises, because an agent with no policy has no identity to compile and nothing
to resume against; contributing the ``policy`` surface is how the brain is
swapped.
"""

from __future__ import annotations

from noeta.builtins._declare import c
from noeta.client.plugin_manifest import PluginManifest


MANIFEST = PluginManifest(
    name="react",
    requires_noeta=">=0.4",
    contributions=(
        c(
            "control_tool",
            "run_workflow",
            "noeta.builtins.react.impl:build_run_workflow_control_tool",
            priority=500,
        ),
        c(
            "control_tool",
            "structured_output",
            "noeta.builtins.react.impl:build_structured_output_control_tool",
            priority=600,
        ),
        c(
            "control_tool",
            "recall_history",
            "noeta.builtins.react.impl:build_recall_history_control_tool",
            priority=550,
        ),
        c(
            "reminder",
            "collapsed-context",
            "noeta.builtins.react.impl:collapsed_context_reminder",
            priority=350,
        ),
    ),
)
