from pathlib import Path
from typing import Any

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


def get_tool_definitions() -> list[dict]:
    tools = [t.to_openai_tool() for t in ALL_TOOLS]
    tools.append({
        "type": "function",
        "function": {
            "name": "exit_plan_mode",
            "description": "Request to exit plan mode and enter build mode. Use this when you have completed your analysis and are ready to make changes. The user must approve the switch.",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    })
    return tools


def execute_tool(
    tool_name: str,
    arguments: dict[str, Any],
    working_dir: Path,
    session_dir: Path,
    additional_dirs: list[Path] | None = None,
) -> dict[str, Any]:
    """Execute a tool and return a result dict.

    Returns dict with at least "result" (str). May include "diff_prev"
    and "diff_current" for write operations.
    """
    tool = _TOOL_MAP.get(tool_name)
    if tool is None:
        return {"result": f"Unknown tool: {tool_name}"}
    result = tool.execute(
        working_dir=working_dir,
        session_dir=session_dir,
        additional_dirs=additional_dirs or [],
        **arguments,
    )
    if isinstance(result, dict):
        return result
    return {"result": result}
