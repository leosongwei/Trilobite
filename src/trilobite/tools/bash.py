import os
import signal
import subprocess
import threading
from functools import lru_cache
from pathlib import Path
from typing import Any, Callable

from src.trilobite.tools.tool import Tool

#: Bubblewrap sandbox for bash: the whole tree is mounted read-only, then the
#: working directory, granted additional directories and the session's tmp
#: directory (bound onto /tmp) are rebound writable. /dev/shm is bound and
#: /dev and /proc must be mounted explicitly inside the sandbox.
#: See doc/product/file_access.md for the full design.
_BWRAP_BASE_ARGS = (
    "--ro-bind", "/", "/",
    "--dev", "/dev",
    "--proc", "/proc",
    "--bind", "/dev/shm", "/dev/shm",
)

#: Denial dialect bubblewrap's kernel speaks when the sandbox blocks a write
#: (EROFS). We append a hint so the model knows the refusal is the sandbox's,
#: not the command's, and how to request write access. Matched in the common
#: locales (bash localizes these messages, e.g. zh_CN "只读文件系统").
_SANDBOX_DENIAL_MARKERS = (
    "Read-only file system",
    "Read-only filesystem",
    "只读文件系统",
)

_SANDBOX_DENIAL_HINT = (
    "\n[sandbox] A write outside the working directory was blocked: bash runs "
    "in a sandbox where everything except the working directory and granted "
    "additional directories is read-only. To write there, first read the "
    "target file with the read tool -- that requests user approval, and once "
    "the directory is granted it becomes writable for bash too."
)


@lru_cache(maxsize=1)
def _bwrap_available() -> bool:
    """Probe whether a working bubblewrap sandbox is available (cached).

    The probe mirrors the real invocation minus the writable bind mounts, so a
    sandbox that passes here is expected to work for actual commands.
    """
    try:
        result = subprocess.run(
            ["bwrap", *_BWRAP_BASE_ARGS, "--die-with-parent", "--", "true"],
            capture_output=True,
            timeout=10,
        )
        return result.returncode == 0
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return False


def _build_bwrap_argv(
    command: str, writable_dirs: list[Path], session_tmp: Path
) -> list[str]:
    """Build the bwrap argv that runs ``command`` under the bash sandbox.

    Mount order matters: the read-only root bind comes first, then each
    writable directory is rebound (later binds override earlier ones). Missing
    or duplicate directories are skipped -- bubblewrap requires bind targets
    to exist. The session's ``tmp/`` directory (``session_tmp``, verified to
    exist by the caller) is bound onto ``/tmp`` so commands get a
    session-scoped scratch area that persists across invocations.
    ``--die-with-parent`` guarantees the sandboxed process tree is torn down
    when the bwrap process itself dies (e.g. our SIGKILL on interrupt/timeout).
    """
    argv = ["bwrap", *_BWRAP_BASE_ARGS, "--bind", str(session_tmp), "/tmp"]
    seen: set[str] = set()
    for directory in writable_dirs:
        try:
            resolved = directory.resolve()
        except OSError:
            continue
        key = str(resolved)
        if key in seen or not resolved.is_dir():
            continue
        seen.add(key)
        argv += ["--bind", key, key]
    argv += ["--die-with-parent", "--", "bash", "-c", command]
    return argv


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
            "description": {
                "type": "string",
                "description": (
                    "Clear, concise description of what this command does in "
                    "active voice, 5-10 words (shown in the UI). Examples: "
                    '"ls" -> "List files in current directory"; "git status" '
                    '-> "Show working tree status"; "npm install" -> "Install '
                    'package dependencies".'
                ),
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
        "required": ["command", "description"],
    }

    def execute(
        self,
        working_dir: Path,
        session_dir: Path,
        additional_dirs: list[Path] | None = None,
        command: str = "",
        description: str = "",
        timeout: int = 10,
        max_output_lines: int = 100,
        max_output_chars: int = 10000,
        config: dict | None = None,
        on_proc: Callable[[subprocess.Popen | None], None] | None = None,
        on_output: Callable[[str, str], None] | None = None,
        **kwargs: Any,
    ) -> str:
        # Use Popen with per-line streaming (instead of subprocess.run /
        # communicate) so the agent can both kill the running process on
        # interrupt *and* stream output to the frontend in real time.
        # start_new_session=True puts the command in its own process group so
        # kill_process_group() can take down the shell's child (e.g. the real
        # ``sleep``) too. The Popen handle is reported through ``on_proc`` so
        # Agent.interrupt() can terminate a long command immediately. Two reader
        # threads drain stdout/stderr line by line, invoking ``on_output`` for
        # each line so the frontend sees live output; lines are also collected
        # so the final returned string still carries the [stderr]/[exit code]
        # markers the model expects.

        # Bash sandboxing (config key ``bash_sandbox``):
        #   auto (default) - use bubblewrap when it probes OK, otherwise run
        #                    unsandboxed with a warning in the result;
        #   on             - require the sandbox; refuse to run without it;
        #   off            - never sandbox.
        # Only the working directory plus granted additional_dirs are writable
        # inside the sandbox; everything else is read-only.
        sandbox_mode = (config or {}).get("bash_sandbox", "auto")
        sandbox_warning = ""
        use_sandbox = False
        if sandbox_mode != "off":
            if _bwrap_available():
                use_sandbox = True
            elif sandbox_mode == "on":
                return (
                    "Error: bash_sandbox=on but bubblewrap (bwrap) is not "
                    "available or failed to probe. Install bubblewrap or set "
                    "bash_sandbox to auto/off in config.yaml."
                )
            else:
                sandbox_warning = (
                    "[sandbox] bubblewrap (bwrap) is not available; bash runs "
                    "without workspace isolation. Install bubblewrap to "
                    "sandbox bash.\n"
                )

        proc: subprocess.Popen | None = None
        try:
            if use_sandbox:
                # The sandbox's /tmp is the session's tmp directory; it must
                # exist before bwrap runs (bind targets are required). If it
                # cannot be created the session directory is broken -- fail
                # loudly instead of masking the problem.
                session_tmp = session_dir / "tmp"
                try:
                    session_tmp.mkdir(parents=True, exist_ok=True)
                except OSError as e:
                    return (
                        f"Error: cannot create the session scratch directory "
                        f"{session_tmp} ({e}). bash needs it as the sandbox's "
                        "/tmp -- check the permissions of the session "
                        "directory and retry."
                    )
                argv = _build_bwrap_argv(
                    command, [working_dir] + list(additional_dirs or []), session_tmp
                )
                shell = False
            else:
                argv = command
                shell = True
            proc = subprocess.Popen(
                argv,
                shell=shell,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                errors="replace",
                cwd=working_dir,
                start_new_session=True,
            )
            if on_proc:
                on_proc(proc)

            stdout_lines: list[str] = []
            stderr_lines: list[str] = []

            def _drain(stream, store: list[str], src: str) -> None:
                for line in iter(stream.readline, ""):
                    line = line.rstrip("\n")
                    store.append(line)
                    if on_output:
                        try:
                            on_output(line, src)
                        except Exception:
                            pass
                stream.close()

            t_out = threading.Thread(
                target=_drain, args=(proc.stdout, stdout_lines, "stdout"), daemon=True)
            t_err = threading.Thread(
                target=_drain, args=(proc.stderr, stderr_lines, "stderr"), daemon=True)
            t_out.start()
            t_err.start()

            try:
                proc.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                kill_process_group(proc)
                proc.wait()
                t_out.join(timeout=1)
                t_err.join(timeout=1)
                return f"Error: Command timed out ({timeout}s)"

            t_out.join()
            t_err.join()

            stdout = "\n".join(stdout_lines)
            stderr = "\n".join(stderr_lines)
            output = stdout
            if stderr:
                if output:
                    output += "\n"
                output += "[stderr]\n" + stderr
            if proc.returncode != 0:
                output += f"\n[exit code: {proc.returncode}]"
            output = output or "(no output)"
            output = truncate_output(output, max_output_lines, max_output_chars)
            if use_sandbox and any(marker in output for marker in _SANDBOX_DENIAL_MARKERS):
                # The kernel's EROFS is the sandbox's refusal, not the
                # command's: tell the model what happened and how to get
                # write access (read tool -> user approval -> additional_dirs).
                output += _SANDBOX_DENIAL_HINT
            elif sandbox_warning:
                output = sandbox_warning + output
            return output
        except Exception as e:
            return f"Error: {e}"
        finally:
            if on_proc:
                on_proc(None)
