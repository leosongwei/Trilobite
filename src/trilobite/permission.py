"""Agent permission policies.

This module exists to keep two different things distinct even though both
look like "which tools are allowed":

* a **mode** -- a runtime state of the *primary* agent, swapped in place
  mid-session (plan <-> build). :class:`PlanModePermission` and
  :class:`BuildModePermission` are modes.
* a **role** -- a declarative profile of a *subagent*, fixed at spawn time
  and never switched (explore / general).
  :class:`ExploreSubagentPermission` and :class:`GeneralSubagentPermission`
  are roles.

Both are expressed as a policy over the tool set, but their lifecycles
differ: a mode is hot-swapped on a running agent, a role is baked in when
the subagent is created. opencode conflates the two with a single
``mode: primary | subagent | all`` field on its agent definitions; we keep
them as sibling subtypes of one abstraction without pretending a mode is
"just another agent definition". The plan/build toggle stays a mode on the
primary agent (see ``doc/product/plan_build_mode.md``); subagent roles are
declared separately and will be spawned by the future ``task`` tool.

A permission has two responsibilities:

1. :meth:`AgentPermission.filter_definitions` -- which tool definitions are
   advertised to the LLM.
2. :meth:`AgentPermission.intercept` -- gate a tool call before execution,
   returning an error message to block it or ``None`` to allow.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from src.trilobite.tool_call import ALL_TOOLS, EXIT_PLAN_MODE_DEF, TASK_TOOL_DEF


class AgentPermission(ABC):
    """Policy over the tool set that an agent run operates under.

    Subclasses declare :attr:`tool_names` (a subset of the global
    :data:`~src.trilobite.tool_call.ALL_TOOLS` names) and implement
    :meth:`intercept`. :meth:`filter_definitions` derives the advertised
    tool list from :attr:`tool_names` plus, optionally, the
    ``exit_plan_mode`` virtual tool.
    """

    #: concrete tool names this policy exposes to the model.
    tool_names: tuple[str, ...] = ()

    #: whether the ``exit_plan_mode`` virtual tool is offered. Only the plan
    #: mode offers it -- it is the in-place transition back to build mode,
    #: so it is a mode concern, not a role concern.
    exposes_exit_plan_mode: bool = False

    #: whether the ``task`` (subagent spawn) tool is offered. Only primary
    #: agents (build/plan modes) offer it; subagent roles never do, which is
    #: what enforces the single-layer nesting limit.
    exposes_task: bool = False

    def filter_definitions(self) -> list[dict]:
        """Tool definitions to send to the LLM for this policy."""
        allowed = set(self.tool_names)
        defs = [t.to_openai_tool() for t in ALL_TOOLS if t.name in allowed]
        if self.exposes_exit_plan_mode:
            defs.append(EXIT_PLAN_MODE_DEF)
        if self.exposes_task:
            defs.append(TASK_TOOL_DEF)
        return defs

    @abstractmethod
    def intercept(self, tool_name: str) -> str | None:
        """Return an error message if ``tool_name`` is blocked, else ``None``.

        This is a defensive gate: tools absent from :attr:`tool_names` are
        never advertised to the model, but a hallucinated call is still
        rejected here with an instructive message rather than a bare
        "unknown tool".
        """
        ...


class BuildModePermission(AgentPermission):
    """Primary agent, build mode: full tool access, can spawn subagents."""

    tool_names = ("read", "write", "bash", "TodoList")
    exposes_task = True

    def intercept(self, tool_name: str) -> str | None:
        return None


class PlanModePermission(AgentPermission):
    """Primary agent, plan mode: read-only, may request an exit to build,
    may spawn read-only (explore) subagents."""

    tool_names = ("read", "bash", "TodoList")
    exposes_exit_plan_mode = True
    exposes_task = True

    def intercept(self, tool_name: str) -> str | None:
        if tool_name == "write":
            return (
                "Error: write tool is not available in plan mode. "
                "Call exit_plan_mode to request switching to build mode."
            )
        return None


class ExploreSubagentPermission(AgentPermission):
    """Read-only exploration subagent.

    A role, not a mode -- fixed at spawn time, never switched. Cannot edit
    files, cannot manage the todo list, and crucially cannot spawn further
    subagents (``task`` is absent), which enforces the single-layer limit.
    """

    tool_names = ("read", "bash")

    def intercept(self, tool_name: str) -> str | None:
        if tool_name == "write":
            return "Error: write tool is not available to the explore subagent."
        if tool_name == "TodoList":
            return "Error: TodoList is not available to subagents."
        return None


class GeneralSubagentPermission(AgentPermission):
    """General-purpose subagent: may edit code, but cannot spawn subagents
    or switch modes.

    A role, not a mode. Unlike the primary build mode it never offers
    ``exit_plan_mode`` (subagents have no plan/build mode of their own) and
    never offers ``TodoList`` (subagents do not maintain the user's todo
    list).
    """

    tool_names = ("read", "write", "bash")

    def intercept(self, tool_name: str) -> str | None:
        if tool_name == "TodoList":
            return "Error: TodoList is not available to subagents."
        return None
