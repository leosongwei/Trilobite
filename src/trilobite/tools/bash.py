import os
import signal
import subprocess
from pathlib import Path
from typing import Any, Callable

from src.trilobite.tools.tool import Tool


def kill_process_group(proc: subprocess.Popen) -> None:
    """Kill the process group of ``proc`` (created with start_new_session).

    With ``shell=True`` the shell spawns the real command as a child that
    inherits the stdout/stderr pipes; killing only the shell leaves that child
    alive and holding the pipes, so ``communicate`` would block until it ends.
    Killing the whole group takes the child down too.
    """
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        try:
            proc.kill()
        except Exception:
            pass
    except Exception:
        pass


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
        on_proc: Callable[[subprocess.Popen | None], None] | None = None,
        **kwargs: Any,
    ) -> str:
        # Use Popen + communicate (instead of subprocess.run) so the agent can
        # kill the running process on interrupt. start_new_session=True puts the
        # command in its own process group so kill_process_group() can take down
        # the shell's child (e.g. the real ``sleep``) too -- otherwise killing
        # only the shell leaves the child holding the pipes and communicate
        # blocks. The Popen handle is reported through ``on_proc`` so
        # Agent.interrupt() can terminate a long command immediately.
        proc: subprocess.Popen | None = None
        try:
            proc = subprocess.Popen(
                command,
                shell=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                cwd=working_dir,
                start_new_session=True,
            )
            if on_proc:
                on_proc(proc)
            try:
                stdout, stderr = proc.communicate(timeout=timeout)
            except subprocess.TimeoutExpired:
                kill_process_group(proc)
                proc.communicate()
                return f"Error: Command timed out ({timeout}s)"
            output = stdout or ""
            if stderr:
                output += "\n[stderr]\n" + stderr
            if proc.returncode != 0:
                output += f"\n[exit code: {proc.returncode}]"
            return output or "(no output)"
        except Exception as e:
            return f"Error: {e}"
        finally:
            if on_proc:
                on_proc(None)
