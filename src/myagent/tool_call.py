from pathlib import Path
from typing import Any

from src.myagent.tools.read import ReadTool
from src.myagent.tools.write import WriteTool
from src.myagent.tools.bash import BashTool
from src.myagent.tools.todo import TodoReadTool, TodoWriteTool, TodoDoneTool

ALL_TOOLS = [
    ReadTool(),
    WriteTool(),
    BashTool(),
    TodoReadTool(),
    TodoWriteTool(),
    TodoDoneTool(),
]

_TOOL_MAP: dict[str, Any] = {t.name: t for t in ALL_TOOLS}


def get_tool_definitions() -> list[dict]:
    return [t.to_openai_tool() for t in ALL_TOOLS]


def execute_tool(tool_name: str, arguments: dict[str, Any], working_dir: Path, session_dir: Path) -> str:
    tool = _TOOL_MAP.get(tool_name)
    if tool is None:
        return f"Unknown tool: {tool_name}"
    return tool.execute(working_dir=working_dir, session_dir=session_dir, **arguments)
