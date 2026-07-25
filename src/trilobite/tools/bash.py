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


def truncate_output(
    text: str, max_lines: int = 100, max_chars: int = 10000
) -> str:
    """Truncate command output to a tail window.

    Bash output is tail-heavy -- errors and final results land at the end --
    so we keep the *last* ``max_lines`` lines and then the *last*
    ``max_chars`` characters. Either limit is disabled by passing ``-1``. A
    note is prepended when truncation happens so the model knows output was
    cut and can raise the limit or page through it.
    """
    truncated = False
    if max_lines != -1:
        lines = text.splitlines(keepends=True)
        if len(lines) > max_lines:
            text = "".join(lines[-max_lines:])
            truncated = True
    if max_chars != -1 and len(text) > max_chars:
        text = text[-max_chars:]
        truncated = True
    if truncated:
        limits = []
        if max_lines != -1:
            limits.append(f"last {max_lines} lines")
        if max_chars != -1:
            limits.append(f"last {max_chars} chars")
        note = (
            "... [output truncated, showing "
            + " / ".join(limits)
            + "; pass max_output_lines=-1 and max_output_chars=-1 to disable]\n"
        )
        text = note + text
    return text


class BashTool(Tool):
    name = "bash"
    description = (
        "Execute a bash command in the working directory. Output is truncated "
        "to the last 100 lines / 10000 chars by default; pass "
        "max_output_lines=-1 and max_output_chars=-1 to disable truncation."
    )
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
            "max_output_lines": {
                "type": "integer",
                "default": 100,
                "description": (
                    "Keep only the last N lines of output. -1 disables the "
                    "line limit (return all lines)."
                ),
            },
            "max_output_chars": {
                "type": "integer",
                "default": 10000,
                "description": (
                    "Keep only the last N characters of output. -1 disables "
                    "the character limit (return full output)."
                ),
            },
        },
        "required": ["command"],
    }

    def execute(
        self,
        working_dir: Path,
        session_dir: Path,
        additional_dirs: list[Path] | None = None,
        command: str = "",
        timeout: int = 10,
        max_output_lines: int = 100,
        max_output_chars: int = 10000,
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
            output = output or "(no output)"
            return truncate_output(output, max_output_lines, max_output_chars)
        except Exception as e:
            return f"Error: {e}"
        finally:
            if on_proc:
                on_proc(None)
