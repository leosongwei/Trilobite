from pathlib import Path
from typing import Any, Callable

from src.trilobite.tools.read import ReadTool
from src.trilobite.tools.edit import EditTool
from src.trilobite.tools.write import WriteTool
from src.trilobite.tools.bash import BashTool
from src.trilobite.tools.glob import GlobTool
from src.trilobite.tools.grep import GrepTool
from src.trilobite.tools.todo import TodoListTool
from src.trilobite.tools.skill import SkillTool

ALL_TOOLS = [
    ReadTool(),
    GlobTool(),
    GrepTool(),
    EditTool(),
    WriteTool(),
    BashTool(),
    TodoListTool(),
    SkillTool(),
]

_TOOL_MAP: dict[str, Any] = {t.name: t for t in ALL_TOOLS}

# Virtual tool: asks the user to switch the primary agent from plan mode to
# build mode. Only exposed by PlanModePermission (see permission.py). Its
# execution -- the approval flow -- is handled in Agent, not here, because it
# needs the broker / asyncio machinery; this is just the definition the LLM
# sees.
EXIT_PLAN_MODE_DEF: dict = {
    "type": "function",
    "function": {
        "name": "exit_plan_mode",
        "description": "Request to exit plan mode and enter build mode. Use this when you have completed your analysis and are ready to make changes. The user must approve the switch.",
        "parameters": {"type": "object", "properties": {}, "required": []},
    },
}

# Virtual tool: spawn one or more subagents to run in parallel. Only exposed by
# BuildModePermission / PlanModePermission (see permission.py). Its execution
# -- creating child Agents, gathering their runs -- is handled in Agent, not
# here; this is just the definition the LLM sees.
TASK_TOOL_DEF: dict = {
    "type": "function",
    "function": {
        "name": "task",
        "description": (
            "Spawn subagents to run tasks in parallel, each with isolated "
            "context. A subagent shields your context - its intermediate work "
            "stays out of your history, so only its final summary costs you "
            "tokens. Use this as the preferred way to explore the codebase or "
            "gather context for any question that is not a needle query for one "
            "specific file/class, and to run independent sub-tasks concurrently "
            "in a single call. Give each a fully self-contained prompt (goal, "
            "relevant file paths, what to return). Skip the subagent for needle "
            "queries: a known file path -> read; one specific definition -> "
            "grep; code within 2-3 known files -> read. The subagents' output "
            "is not shown to the user -- you must relay/summarize their results "
            "yourself."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "tasks": {
                    "type": "array",
                    "description": "One or more subtasks to run in parallel.",
                    "items": {
                        "type": "object",
                        "properties": {
                            "description": {
                                "type": "string",
                                "description": "A 3-5 word label for the subtask, used as the subagent session title.",
                            },
                            "subagent_type": {
                                "type": "string",
                                "enum": ["explore", "general"],
                                "description": "explore = read-only (read/bash); general = can edit code (read/edit/write/bash).",
                            },
                            "prompt": {
                                "type": "string",
                                "description": "Fully self-contained task instruction: the goal, relevant paths, and exactly what to return.",
                            },
                        },
                        "required": ["description", "subagent_type", "prompt"],
                    },
                }
            },
            "required": ["tasks"],
        },
    },
}


# Virtual tool: suspend this session until a target time. Exposed by the
# primary modes (build/plan); its execution -- registering the suspension
# with the TimerService -- is handled in Agent, not here. Waking re-enters
# this same conversation with a synthetic wake-up message.
SLEEP_UNTIL_DEF: dict = {
    "type": "function",
    "function": {
        "name": "sleep_until",
        "description": (
            "Suspend this session until a target time, then resume this "
            "conversation automatically. No tokens are spent while suspended. "
            "Use it when future work depends on time passing: waiting for a "
            "build/CI to finish, resuming a task later or tomorrow, reminding "
            "the user at a specific moment, or polling on an interval (wake, "
            "check, sleep again). Other tool calls in the same turn run to "
            "completion before the suspension starts, so finish everything "
            "you can do now before sleeping."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "until": {
                    "type": "string",
                    "description": (
                        "Target time, local timezone. Formats: relative "
                        "'+30m' / '+2h' / '+1d' / '+90s' (a bare '+30' means "
                        "minutes) -- preferred, since you may not know the "
                        "current time; absolute 'YYYY-MM-DD HH:MM'; 'MM-DD "
                        "HH:MM' (this year, next year if already past); "
                        "'HH:MM' (today, tomorrow if already past). Must be "
                        "5s to 365d from now; parse errors include the "
                        "current local time."
                    ),
                },
            },
            "required": ["until"],
        },
    },
}


def execute_tool(
    tool_name: str,
    arguments: dict[str, Any],
    working_dir: Path,
    session_dir: Path,
    additional_dirs: list[Path] | None = None,
    config: dict | None = None,
    on_proc: Callable[[Any], None] | None = None,
    on_output: Callable[[str, str], None] | None = None,
) -> dict[str, Any]:
    """Execute a tool and return a result dict.

    Returns dict with at least "result" (str). May include "diff" (a list of
    {type, old, new, text} rows with real line numbers) for edit operations.

    ``on_proc`` is forwarded to tools that spawn a subprocess (bash) so the
    caller (Agent) can kill the process on interrupt; other tools ignore it.
    ``on_output`` is forwarded to bash so each stdout/stderr line can be
    streamed to the frontend in real time; other tools ignore it.
    ``config`` is forwarded to bash so it can honor the ``bash_sandbox``
    setting; other tools ignore it.
    """
    tool = _TOOL_MAP.get(tool_name)
    if tool is None:
        return {"result": f"Unknown tool: {tool_name}"}
    kwargs: dict[str, Any] = {}
    if tool_name == "bash":
        kwargs["config"] = config
    result = tool.execute(
        working_dir=working_dir,
        session_dir=session_dir,
        additional_dirs=additional_dirs or [],
        on_proc=on_proc,
        on_output=on_output,
        **kwargs,
        **arguments,
    )
    if isinstance(result, dict):
        return result
    return {"result": result}
