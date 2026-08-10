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


# Virtual tool: schedule a prompt to run later as an unattended scheduled
# agent (cron). Only exposed by the primary modes (build/plan); its execution
# -- creating the schedule, firing scheduled agents on time -- is handled by
# the CronService via Agent, not here. There is deliberately no "edit" tool:
# schedules are immutable once created (adjust by delete + create).
CRON_CREATE_DEF: dict = {
    "type": "function",
    "function": {
        "name": "cron_create",
        "description": (
            "Schedule a prompt to run later as an unattended scheduled agent. "
            "The agent fires at the cron time with a FRESH context (only this "
            "prompt), runs to completion, and its results are NOT returned to "
            "you -- view runs in the sidebar under the schedule's session. "
            "Use for periodic/unattended work: daily checks that write to a "
            "file, reminders, recurring reports. The scheduled agent is "
            "general-role: it can edit files in the workspace. In plan mode "
            "this tool is blocked. Use recurring=false for a one-shot "
            "'remind me at <time>' schedule. Adjust a schedule by deleting "
            "and recreating it."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "cron": {
                    "type": "string",
                    "description": "5-field cron expression in local time: 'minute hour day-of-month month day-of-week' (e.g. '30 9 * * *' = every day at 09:30; '*/5 * * * *' = every 5 minutes).",
                },
                "prompt": {
                    "type": "string",
                    "description": "Self-contained task prompt (max 8 KiB) injected as the scheduled agent's only instruction at each fire.",
                },
                "recurring": {
                    "type": "boolean",
                    "description": "true (default) = fire on every cron match until deleted. false = fire once at the next match, then auto-delete.",
                },
            },
            "required": ["cron", "prompt"],
        },
    },
}

CRON_LIST_DEF: dict = {
    "type": "function",
    "function": {
        "name": "cron_list",
        "description": "List this session's cron schedules (id, cron expression, recurring, run count, last state, next fire time). Use the ids with cron_delete.",
        "parameters": {"type": "object", "properties": {}, "required": []},
    },
}

CRON_DELETE_DEF: dict = {
    "type": "function",
    "function": {
        "name": "cron_delete",
        "description": "Delete a cron schedule by id (from cron_list / cron_create). Future fires stop; the schedule's session stays in the sidebar for reviewing past runs.",
        "parameters": {
            "type": "object",
            "properties": {
                "id": {"type": "string", "description": "Schedule id to delete."},
            },
            "required": ["id"],
        },
    },
}

CRON_TOOL_DEFS: tuple[dict, ...] = (CRON_CREATE_DEF, CRON_LIST_DEF, CRON_DELETE_DEF)


def execute_tool(
    tool_name: str,
    arguments: dict[str, Any],
    working_dir: Path,
    session_dir: Path,
    additional_dirs: list[Path] | None = None,
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
    """
    tool = _TOOL_MAP.get(tool_name)
    if tool is None:
        return {"result": f"Unknown tool: {tool_name}"}
    result = tool.execute(
        working_dir=working_dir,
        session_dir=session_dir,
        additional_dirs=additional_dirs or [],
        on_proc=on_proc,
        on_output=on_output,
        **arguments,
    )
    if isinstance(result, dict):
        return result
    return {"result": result}
