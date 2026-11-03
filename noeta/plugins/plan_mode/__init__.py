"""Plan mode.

* :class:`PlanMode` — Claude Code's plan mode: ``/plan``, ``EnterPlanMode``
  and ``ExitPlanMode`` (the approval question), the plan file and its name,
  and the plan-mode notes. The task's mode stays the permissions plugin's
  ``mode`` record; with that plugin it judges what may run while planning,
  without it this plugin keeps planning read-only by itself.
"""

from noeta.plugins.plan_mode.extension import PlanMode
from noeta.plugins.plan_mode.plan import ENTER_PLAN_MODE, EXIT_PLAN_MODE
from noeta.plugins.plan_mode.state import PlanState

__all__ = ["ENTER_PLAN_MODE", "EXIT_PLAN_MODE", "PlanMode", "PlanState"]
