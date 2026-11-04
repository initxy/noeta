"""What Claude Code's harness tells the model about where it runs, in the
same places: a git snapshot and the commit-attribution note in the first
user turn, the environment (with the model line, the deferred tools, the
token budget and today's date) as a system turn right after it, and a
``<total_tokens>`` budget note after every prompt and tool result (a
sub-agent's after its tool results). As in Claude Code, only a model Claude
Code knows as a Claude model gets the budget (:func:`gets_budget`): for any
other model, and with ``token_budget=0``, no note is sent and the prompt's
budget line is taken out wherever it sits.

Everything is captured through the task's ExecEnv (``view.env``), never by a
host subprocess: on a container the facts describe the container's checkout and platform,
and the shell / OS version lines (facts of this machine) are left out. git
runs with the hardening the Bash tool's ``git status`` rule applies (``-c
core.fsmonitor=``, no hooks), no signature program and no look inside
submodules. A ``filter.<driver>.clean`` program has no off
switch, so when the repository's own config (local or worktree scope)
defines any filter driver the status is skipped. The capture is recorded
when the task first starts, and taken again for a compaction (which starts
the conversation over from its summary, as Claude Code does); in between it
never changes under the prompt cache. noeta's
own trigger for taking it again: Claude Code re-announces a changed session
context when a session resumes (a new process); a durable task has no resume
moment, so at the start of each later turn the branch and HEAD commit are
compared with what they were when the previous turn ended, and when someone
moved them in between (another branch, new commits) the new snapshot is
given in that turn. The model's own commits during a turn do not count.

As in Claude Code, a switched model is told before its next call, a change
of the repository, worktree or additional-directory facts at the start of
the next turn ("# Environment update"), and the ``language`` setting as its
own note; the main prompt gets Fable 5.1's and Haiku 5.5's own paragraphs
(:func:`with_model_paragraphs`).
"""

from noeta.plugins.environment.extension import (
    GIT_RECAPTURE_TIMEOUT_S,
    ORDER_BUDGET,
    ORDER_DATE,
    ORDER_DEFERRED,
    ORDER_DEFERRED_SUBAGENT,
    ORDER_ENVIRONMENT,
    ORDER_ENVIRONMENT_AFTER_COMPACTION,
    ORDER_LANGUAGE,
    Environment,
)
from noeta.plugins.environment.facts import (
    CLAUDE_COAUTHOR_EMAIL,
    ENVIRONMENT_UPDATE,
    NO_ATTRIBUTION,
    NOT_WORKTREE_LINE,
    PROJECT_URL,
    STASH_LINE,
    WORKTREE_LINE,
    capture_environment,
    capture_facts,
    model_line,
    render_attribution,
    render_environment,
    render_environment_update,
    short_name,
)
from noeta.plugins.environment.git import (
    GIT_STATUS_CUT_NOTE,
    GIT_STATUS_MAX_CHARS,
    RECENT_COMMITS,
    SNAPSHOT_AGAIN,
    SNAPSHOT_AT_START,
    capture_git,
    git_head,
    git_root,
    is_worktree,
    render_git,
)
from noeta.plugins.environment.notes import (
    DATE_CHANGED_REMINDER,
    DATE_CHANGED_UTC_REMINDER,
    DEFERRED_BACK,
    DEFERRED_GONE,
    DEFERRED_GONE_MCP,
    DEFERRED_TOOLS_HEAD,
    EARLY_STOPPING,
    FABLE_IDENTITY,
    LANGUAGE_CLEARED,
    LANGUAGE_NOTE,
    MODEL_CHANGED,
    TOKEN_BUDGET,
    budget_note,
    date_line,
    deferred_delta,
    deferred_list,
    gets_budget,
    today_in,
    valid_zone,
    with_model_paragraphs,
)

__all__ = [
    "CLAUDE_COAUTHOR_EMAIL",
    "DATE_CHANGED_REMINDER",
    "DATE_CHANGED_UTC_REMINDER",
    "DEFERRED_BACK",
    "DEFERRED_GONE",
    "DEFERRED_GONE_MCP",
    "DEFERRED_TOOLS_HEAD",
    "EARLY_STOPPING",
    "ENVIRONMENT_UPDATE",
    "FABLE_IDENTITY",
    "GIT_RECAPTURE_TIMEOUT_S",
    "GIT_STATUS_CUT_NOTE",
    "GIT_STATUS_MAX_CHARS",
    "LANGUAGE_CLEARED",
    "LANGUAGE_NOTE",
    "MODEL_CHANGED",
    "NOT_WORKTREE_LINE",
    "NO_ATTRIBUTION",
    "ORDER_BUDGET",
    "ORDER_DATE",
    "ORDER_DEFERRED",
    "ORDER_DEFERRED_SUBAGENT",
    "ORDER_ENVIRONMENT",
    "ORDER_ENVIRONMENT_AFTER_COMPACTION",
    "ORDER_LANGUAGE",
    "PROJECT_URL",
    "RECENT_COMMITS",
    "SNAPSHOT_AGAIN",
    "SNAPSHOT_AT_START",
    "STASH_LINE",
    "TOKEN_BUDGET",
    "WORKTREE_LINE",
    "Environment",
    "budget_note",
    "capture_environment",
    "capture_facts",
    "capture_git",
    "date_line",
    "deferred_delta",
    "deferred_list",
    "gets_budget",
    "git_head",
    "git_root",
    "is_worktree",
    "model_line",
    "render_attribution",
    "render_environment",
    "render_environment_update",
    "render_git",
    "short_name",
    "today_in",
    "valid_zone",
    "with_model_paragraphs",
]
