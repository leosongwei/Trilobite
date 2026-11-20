"""Agent permission policies.

This module keeps two kinds of policy apart even though both look like
"which tools are allowed":

* the **primary agent** policy -- full tool access, owns the virtual
  ``task`` and ``sleep_until`` tools.
* a **role** -- a declarative profile of a *subagent*, fixed at spawn time
  and never switched (explore / general). :class:`ExploreSubagentPermission`
  and :class:`GeneralSubagentPermission` are roles.

Both are expressed as a policy over the tool set, but their lifecycles
differ: the primary policy is the default of a running agent, a role is
baked in when the subagent is created. opencode conflates the two with a
single ``mode: primary | subagent | all`` field on its agent definitions;
we keep them as sibling subtypes of one abstraction. Subagent roles are
declared separately and spawned by the ``task`` tool.

A permission has two responsibilities:

1. :meth:`AgentPermission.filter_definitions` -- which tool definitions are
   advertised to the LLM.
2. :meth:`AgentPermission.intercept` -- gate a tool call before execution,
   returning an error message to block it or ``None`` to allow.

:attr:`AgentPermission.tool_names` is the single source of truth for the
intercept gate: a policy declares the tools it *allows*, and the generic
:meth:`AgentPermission.intercept` blocks anything outside that set. What
gets advertised to the model defaults to the same set
(:attr:`AgentPermission.advertised_tool_names`). For the primary policy the
advertised set equals the allowed set, so :meth:`intercept` is a defensive
backstop; subagent roles expose exactly what they allow.
"""

from abc import ABC

from src.trilobite.tool_call import (
    ALL_TOOLS,
    SLEEP_UNTIL_DEF,
    TASK_TOOL_DEF,
)

#: every concrete tool name, in :data:`~src.trilobite.tool_call.ALL_TOOLS`
#: order. The primary policy advertises this full set.
ALL_TOOL_NAMES: tuple[str, ...] = tuple(t.name for t in ALL_TOOLS)

#: virtual sleep_until tool name (see SLEEP_UNTIL_DEF in tool_call.py).
SLEEP_TOOL_NAME = "sleep_until"


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
    #: (advertise what you allow).
    advertised_tool_names: tuple[str, ...] | None = None

    #: message template for blocked tools; ``{tool}`` is replaced with the
    #: tool name. Subclasses override to tailor the hint.
    block_message: str = "Error: {tool} tool is not available in the current mode."

    #: whether the ``task`` (subagent spawn) tool is offered. Only the
    #: primary agent offers it; subagent roles keep it out of the tool set,
    #: which enforces the single-layer nesting limit.
    exposes_task: bool = False

    #: whether the ``sleep_until`` (timer suspension) virtual tool is
    #: offered. The primary agent offers it -- it has no side effects.
    #: Subagent roles keep it out: a bounded sub-task must not park itself
    #: while the parent waits on its result.
    exposes_sleep: bool = False

    def filter_definitions(self, enable_vl: bool = False) -> list[dict]:
        """Tool definitions to send to the LLM for this policy."""
        advertised = set(self.advertised_tool_names or self.tool_names)
        defs = [t.to_openai_tool(enable_vl) for t in ALL_TOOLS if t.name in advertised]
        if self.exposes_task:
            defs.append(TASK_TOOL_DEF)
        if self.exposes_sleep:
            defs.append(SLEEP_UNTIL_DEF)
        return defs

    def intercept(self, tool_name: str) -> str | None:
        """Return an error message if ``tool_name`` is blocked, else ``None``.

        The decision is read from :attr:`tool_names` and the virtual-tool
        flags -- a policy declares what it allows and this gate enforces
        exactly that. For the primary policy (allowed set equals the
        advertised set) this is a defensive backstop.
        """
        if tool_name in self.tool_names:
            return None
        if tool_name == "task" and self.exposes_task:
            return None
        if tool_name == SLEEP_TOOL_NAME and self.exposes_sleep:
            return None
        return self.block_message.format(tool=tool_name)


class PrimaryPermission(AgentPermission):
    """Primary agent: full tool access, can spawn subagents and suspend.

    The allowed set is the full set, so the generic :meth:`intercept` passes
    every tool.
    """

    tool_names = ALL_TOOL_NAMES
    exposes_task = True
    exposes_sleep = True


class ExploreSubagentPermission(AgentPermission):
    """Read-only exploration subagent.

    A role, not a mode -- fixed at spawn time, never switched. Cannot edit
    files, cannot manage the todo list, and crucially cannot spawn further
    subagents (``task`` is absent), which enforces the single-layer limit.
    """

    tool_names = ("read", "glob", "grep", "bash", "skill")

    block_message = "Error: {tool} tool is not available to the explore subagent."


class GeneralSubagentPermission(AgentPermission):
    """General-purpose subagent: may edit code, but cannot spawn subagents.

    A role, not a mode. Unlike the primary policy it never offers ``task``
    (single-layer nesting) and never offers ``TodoList`` (subagents do not
    maintain the user's todo list).
    """

    tool_names = ("read", "glob", "grep", "edit", "write", "bash", "skill")

    block_message = "Error: {tool} tool is not available to subagents."
