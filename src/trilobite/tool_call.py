from pathlib import Path
from typing import Any, Callable

from src.trilobite.tools.read import ReadTool
from src.trilobite.tools.write import WriteTool
from src.trilobite.tools.bash import BashTool
from src.trilobite.tools.todo import TodoListTool

ALL_TOOLS = [
    ReadTool(),
    WriteTool(),
    BashTool(),
    TodoListTool(),
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
            "Spawn subagents to run tasks in parallel with isolated context. "
            "Each subagent runs independently and returns only its final summary; "
            "its intermediate work is invisible to you, so give each a fully "
            "self-contained prompt (goal, relevant file paths, what to return). "
            "Use this to offload context-heavy exploration/search, or to run "
            "independent sub-tasks concurrently. Do NOT use it for anything a "
            "single read/bash could do. The subagents' output is not shown to "
            "the user -- you must relay/summarize their results yourself."
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
                                "description": "explore = read-only (read/bash); general = can edit code (read/write/bash).",
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


def execute_tool(
    tool_name: str,
    arguments: dict[str, Any],
    working_dir: Path,
    session_dir: Path,
    additional_dirs: list[Path] | None = None,
    on_proc: Callable[[Any], None] | None = None,
) -> dict[str, Any]:
    """Execute a tool and return a result dict.

    Returns dict with at least "result" (str). May include "diff_prev"
    and "diff_current" for write operations.

    ``on_proc`` is forwarded to tools that spawn a subprocess (bash) so the
    caller (Agent) can kill the process on interrupt; other tools ignore it.
    """
    tool = _TOOL_MAP.get(tool_name)
    if tool is None:
        return {"result": f"Unknown tool: {tool_name}"}
    result = tool.execute(
        working_dir=working_dir,
        session_dir=session_dir,
        additional_dirs=additional_dirs or [],
        on_proc=on_proc,
        **arguments,
    )
    if isinstance(result, dict):
        return result
    return {"result": result}
