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

:attr:`AgentPermission.tool_names` is the single source of truth for the
intercept gate: a policy declares the tools it *allows*, and the generic
:meth:`AgentPermission.intercept` blocks anything outside that set. What
gets advertised to the model defaults to the same set
(:attr:`AgentPermission.advertised_tool_names`); the primary modes override
it with the full set for cache stability, and the mode difference is
enforced by :meth:`intercept`.
"""

from abc import ABC

from src.trilobite.tool_call import ALL_TOOLS, EXIT_PLAN_MODE_DEF, TASK_TOOL_DEF

#: every concrete tool name, in :data:`~src.trilobite.tool_call.ALL_TOOLS`
#: order. The primary modes advertise this full set (see
#: :attr:`AgentPermission.advertised_tool_names`) so the tools prefix is
#: byte-identical across plan/build switches (cache stability).
ALL_TOOL_NAMES: tuple[str, ...] = tuple(t.name for t in ALL_TOOLS)


class AgentPermission(ABC):
    """Policy over the tool set that an agent run operates under.

    Subclasses declare :attr:`tool_names` (the concrete tools this policy
    allows) plus the virtual-tool flags; the generic :meth:`intercept`
    derives its decision from those declarations.
    :meth:`filter_definitions` advertises :attr:`advertised_tool_names`,
    whose default is :attr:`tool_names`.
    """

    #: concrete tool names this policy ALLOWS at execution time. The generic
    #: :meth:`intercept` gate blocks any tool outside this set.
    tool_names: tuple[str, ...] = ()

    #: tool names advertised to the LLM. The default is :attr:`tool_names`
    #: (advertise what you allow). The primary modes override this with
    #: :data:`ALL_TOOL_NAMES` so the tools prefix is identical across
    #: plan/build switches (cache stability); the mode difference is
    #: enforced by :meth:`intercept`.
    advertised_tool_names: tuple[str, ...] | None = None

    #: message template for blocked tools; ``{tool}`` is replaced with the
    #: tool name. Subclasses override to tailor the hint.
    block_message: str = "Error: {tool} tool is not available in the current mode."

    #: whether the ``exit_plan_mode`` virtual tool is offered. Both primary
    #: modes advertise it (for tool-prefix cache stability across mode
    #: switches); subagent roles keep it out of the tool set. In build mode
    #: calls to it receive a no-op result at dispatch time.
    exposes_exit_plan_mode: bool = False

    #: whether the ``task`` (subagent spawn) tool is offered. Only primary
    #: agents (build/plan modes) offer it; subagent roles keep it out of the
    #: tool set, which enforces the single-layer nesting limit.
    exposes_task: bool = False

    def filter_definitions(self, enable_vl: bool = False) -> list[dict]:
        """Tool definitions to send to the LLM for this policy."""
        advertised = set(self.advertised_tool_names or self.tool_names)
        defs = [t.to_openai_tool(enable_vl) for t in ALL_TOOLS if t.name in advertised]
        if self.exposes_exit_plan_mode:
            defs.append(EXIT_PLAN_MODE_DEF)
        if self.exposes_task:
            defs.append(TASK_TOOL_DEF)
        return defs

    def intercept(self, tool_name: str) -> str | None:
        """Return an error message if ``tool_name`` is blocked, else ``None``.

        The decision is read from :attr:`tool_names` and the virtual-tool
        flags -- a policy declares what it allows and this gate enforces
        exactly that. In plan mode it is the *primary* gate: ``edit``/``write``
        stay advertised for cache stability and are blocked here. In subagent
        roles the advertised set equals the allowed set, so this gate is a
        defensive backstop.
        """
        if tool_name in self.tool_names:
            return None
        if tool_name == "exit_plan_mode" and self.exposes_exit_plan_mode:
            return None
        if tool_name == "task" and self.exposes_task:
            return None
        return self.block_message.format(tool=tool_name)


class BuildModePermission(AgentPermission):
    """Primary agent, build mode: full tool access, can spawn subagents.

    Advertises the same full tool set as :class:`PlanModePermission` so the
    tools prefix is byte-identical across mode switches (cache-stable). The
    allowed set is the full set, so the generic :meth:`intercept` passes
    every tool. ``exit_plan_mode`` is advertised; calls to it receive a
    no-op result at dispatch time (see :class:`Agent`).
    """

    tool_names = ALL_TOOL_NAMES
    exposes_exit_plan_mode = True
    exposes_task = True


class PlanModePermission(AgentPermission):
    """Primary agent, plan mode: read-only, may request an exit to build,
    may spawn read-only (explore) subagents.

    Advertises the same full tool set as :class:`BuildModePermission` so the
    tools prefix is byte-identical across mode switches (cache-stable). The
    allowed set (:attr:`tool_names`) is the read-only subset; the generic
    :meth:`intercept` rejects ``edit``/``write``, and the ``<modeswitch>``
    notice tells the model they are blocked.
    """

    tool_names = ("read", "glob", "grep", "bash", "TodoList")
    advertised_tool_names = ALL_TOOL_NAMES
    exposes_exit_plan_mode = True
    exposes_task = True

    block_message = (
        "Error: {tool} tool is blocked in plan mode. "
        "Call exit_plan_mode to request switching to build mode."
    )


class ExploreSubagentPermission(AgentPermission):
    """Read-only exploration subagent.

    A role, not a mode -- fixed at spawn time, never switched. Cannot edit
    files, cannot manage the todo list, and crucially cannot spawn further
    subagents (``task`` is absent), which enforces the single-layer limit.
    """

    tool_names = ("read", "glob", "grep", "bash")

    block_message = "Error: {tool} tool is not available to the explore subagent."


class GeneralSubagentPermission(AgentPermission):
    """General-purpose subagent: may edit code, but cannot spawn subagents
    or switch modes.

    A role, not a mode. Unlike the primary build mode it never offers
    ``exit_plan_mode`` (subagents have no plan/build mode of their own) and
    never offers ``TodoList`` (subagents do not maintain the user's todo
    list).
    """

    tool_names = ("read", "glob", "grep", "edit", "write", "bash")

    block_message = "Error: {tool} tool is not available to subagents."
