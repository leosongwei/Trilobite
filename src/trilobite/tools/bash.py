import subprocess
from pathlib import Path
from typing import Any

from src.trilobite.tools.tool import Tool


class BashTool(Tool):
    name = "bash"
    description = "Execute a bash command in the working directory."
    parameters = {
        "type": "object",
        "properties": {
            "command": {
                "type": "string",
                "description": "The bash command to execute.",
            },
            "timeout": {
                "type": "integer",
                "description": "Timeout in seconds (default 10).",
            },
        },
        "required": ["command"],
    }

    def execute(
        self,
        working_dir: Path,
        session_dir: Path,
        command: str = "",
        timeout: int = 10,
        **kwargs: Any,
    ) -> str:
        try:
            result = subprocess.run(
                command,
                shell=True,
                capture_output=True,
                text=True,
                timeout=timeout,
                cwd=working_dir,
            )
            output = result.stdout
            if result.stderr:
                output += "\n[stderr]\n" + result.stderr
            if result.returncode != 0:
                output += f"\n[exit code: {result.returncode}]"
            return output or "(no output)"
        except subprocess.TimeoutExpired:
            return f"Error: Command timed out ({timeout}s)"
        except Exception as e:
            return f"Error: {e}"
