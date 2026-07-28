"""Command-line interactive mode for Trilobite.

A non-HTTP, bash-style REPL that drives the same ``Agent`` the web server
uses. The CLI is a pure subscriber + renderer: it instantiates an ``Agent``,
subscribes to its ``StreamBroker`` queue (exactly like the SSE ``/stream``
endpoint) and renders events to the terminal. No agent / broker / tool logic
is duplicated.

Run via ``trilobite -c [working_dir]`` (wired up in ``server.main``).
"""

import asyncio
import json
import os
import re
import signal
import sys
import time
import uuid
from pathlib import Path

try:
    import readline  # noqa: F401  enables line editing (arrows/backspace/history) for input()
except ImportError:  # non-readline platforms (e.g. Windows)
    pass

from src.trilobite.agent import Agent
from src.trilobite.config import get_sessions_dir, init_config


# --- ANSI colors -------------------------------------------------------------

_COLOR = sys.stdout.isatty()


def _ansi(codes: str, text: str) -> str:
    return f"\033[{codes}m{text}\033[0m" if _COLOR else text


def _blue(text: str) -> str:
    return _ansi("34", text)


def _green(text: str) -> str:
    return _ansi("32", text)


def _red(text: str) -> str:
    return _ansi("31", text)


def _yellow(text: str) -> str:
    return _ansi("33", text)


def _dim(text: str) -> str:
    return _ansi("2", text)


def _yellow_dim(text: str) -> str:
    return _ansi("2;33", text)


def _orange(text: str) -> str:
    # Matches the web ToolEntry .tool-action color (#ce9178).
    return _ansi("38;2;206;145;120", text)


def _white(text: str) -> str:
    # Force white explicitly: terminal default foreground varies by user config.
    return _ansi("37", text)


def _gray(text: str) -> str:
    # Dark gray for thinking; \033[2m (faint) is ignored/too bright on some terminals.
    return _ansi("38;2;110;118;129", text)


def _rl_wrap(text: str) -> str:
    """Wrap ANSI escapes with readline's \\001..\\002 markers so readline
    excludes them from cursor-width math. Without this, colored prompts throw
    off the cursor position and break line editing."""
    if not _COLOR:
        return text
    return re.sub(r"(\033\[[0-9;]*m)", lambda m: f"\001{m.group(1)}\002", text)


# --- event classification ----------------------------------------------------

_TERMINAL = {"done", "cancelled", "error", "interrupted"}
_INTERACTIVE = {"permission_request", "plan_exit_request", "subagent_permission_request"}
_EXIT_CODE_RE = re.compile(r"\[exit code: (-?\d+)\]")


# --- tool call / diff formatting --------------------------------------------

def _tool_call_str(tool: str, args: dict) -> str:
    """One-line tool label, matching the web ToolEntry ``label`` format."""
    if tool == "bash" and args.get("command"):
        return f"bash: {args['command']}"
    if tool == "read" and args.get("filename"):
        return f"read: {args['filename']}"
    if tool == "edit" and args.get("filename"):
        return f"edit: {args['filename']}"
    if tool == "write" and args.get("filename"):
        return f"write: {args['filename']}"
    if tool == "glob" and args.get("pattern"):
        return f"glob: {args['pattern']}" + (f" in {args['path']}" if args.get("path") else "")
    if tool == "grep" and args.get("pattern"):
        s = f"grep: {args['pattern']}"
        if args.get("glob"):
            s += f" ({args['glob']})"
        if args.get("path"):
            s += f" in {args['path']}"
        return s
    if tool == "task":
        tasks = args.get("tasks") or []
        return f"task: {len(tasks)} subagent" + ("" if len(tasks) == 1 else "s")
    return tool


def _line_no(n) -> str:
    return str(n) if n is not None else ""


def _usage_text(token_count: int, max_tokens: int) -> str:
    """Token-usage line, matching the web TokenBar format."""
    if max_tokens > 0:
        pct = token_count / max_tokens * 100
        return f"Tokens: {token_count:,} / {max_tokens:,} ({pct:.1f}%)"
    if token_count > 0:
        return f"Tokens: {token_count:,}"
    return "Tokens: -"


# --- renderer ----------------------------------------------------------------

class Renderer:
    """Renders broker events to stdout, tracking whether the cursor sits at the
    start of a line so block events can be placed on their own line."""

    def __init__(self) -> None:
        self.at_line_start = True
        self._subagent_desc: dict[str, str] = {}
        # Last streaming section ("thinking" / "text" / None) so we can insert a
        # blank separator when the model switches from reasoning to its reply.
        self._last_stream: str | None = None

    def write(self, text: str) -> None:
        sys.stdout.write(text)
        self.at_line_start = text.endswith("\n")
        sys.stdout.flush()

    def ensure_newline(self) -> None:
        if not self.at_line_start:
            self.write("\n")

    def block(self, text: str) -> None:
        """Emit a block (own line): ensure we're at line start, then write."""
        self.ensure_newline()
        self.write(text)

    def render(self, ev: dict) -> None:
        t = ev.get("type")
        if t == "thinking":
            self.write(_gray(ev.get("text", "")))
            self._last_stream = "thinking"
        elif t == "text":
            # Separate the reasoning from the reply: if thinking just streamed
            # without a trailing newline, start the reply on its own line.
            if self._last_stream == "thinking":
                self.ensure_newline()
            self.write(ev.get("text", ""))
            self._last_stream = "text"
        elif t == "tool_output":
            # bash streams complete lines (newline already stripped upstream).
            self.write(_white(ev.get("text", "")) + "\n")
        elif t == "tool_start":
            self.block(_orange(f"[{_tool_call_str(ev.get('tool', '?'), ev.get('args') or {})}]") + "\n")
        elif t == "tool_result":
            self._render_tool_result(ev)
        elif t == "usage":
            self.block(_dim(_usage_text(ev.get("token_count", 0), ev.get("max_context_tokens", 0)) + "\n"))
        elif t == "status":
            self.block(_yellow_dim(ev.get("text", "") + "\n"))
        elif t == "compact":
            self.block(_dim("── context compacted ──\n"))
        elif t == "subagents":
            self.ensure_newline()
            for child in ev.get("children", []):
                desc = child.get("description") or child.get("session", "")
                self._subagent_desc[child.get("session", "")] = desc
                self.write(_dim(f"agent: {desc} 启动\n"))
        elif t == "subagent_state":
            sess = ev.get("session", "")
            desc = self._subagent_desc.get(sess, sess)
            self.block(_dim(f"agent: {desc} 退出 ({ev.get('state', '')})\n"))
        elif t == "cancelled":
            self.block(_dim("── cancelled ──\n"))
        elif t == "interrupted":
            self.block(_dim("── interrupted ──\n"))
        elif t == "error":
            self.block(_red(f"✗ {ev.get('text', '')}\n"))
        # init / user / turn / tool_stream / done: skipped

    def _render_tool_result(self, ev: dict) -> None:
        tool = ev.get("tool", "")
        if tool == "edit" and "diff" in ev:
            self.ensure_newline()
            self._render_diff(ev["diff"])
            return
        if tool == "bash":
            # Output was already streamed via tool_output; surface only a
            # non-zero exit code.
            m = _EXIT_CODE_RE.search(ev.get("result", ""))
            if m and int(m.group(1)) != 0:
                self.block(_red(f"✗ exit code: {m.group(1)}\n"))
            return
        if tool == "task":
            # Subagent start/exit are already shown via subagents /
            # subagent_state events; the aggregated <task_result> conclusion is
            # suppressed.
            return
        text = ev.get("result", "")
        self.block(_white(text) + ("" if text.endswith("\n") else "\n"))

    def _render_diff(self, diff: list) -> None:
        for row in diff:
            typ = row.get("type")
            text = row.get("text", "")
            if typ == "added":
                self.write(_green(f"{_line_no(row.get('new'))} + {text}\n"))
            elif typ == "removed":
                self.write(_red(f"{_line_no(row.get('old'))} - {text}\n"))
            else:  # equal
                self.write(_dim(f"{_line_no(row.get('new') or row.get('old'))}   {text}\n"))


# --- interactive prompts (permission / plan exit) ---------------------------

def _resolve(ev: dict, agent: Agent, approved: bool) -> None:
    if ev.get("type") == "plan_exit_request":
        agent.resolve_plan_exit(approved)
    else:
        agent.resolve_permission(approved)


async def _handle_interactive(agent: Agent, ev: dict, renderer: Renderer) -> str:
    """Prompt for a y/n answer to a permission / plan-exit request.

    The agent is blocked on its resolution event during this call, so no other
    output streams compete for the terminal. Returns "exit" if stdin hit EOF
    (caller cancels + exits the CLI), "continue" otherwise.

    RUNNING keeps ``loop.add_signal_handler(SIGINT, agent.cancel)`` active, but
    that handler swallows Ctrl+C during a blocking ``input()``: readline defers
    to the installed Python signal handler, which only schedules a loop callback
    that cannot run while input() has the loop parked. So we drop the loop
    handler for the prompt (restoring Python's default SIGINT -> KeyboardInterrupt),
    then re-install it afterwards.
    """
    loop = asyncio.get_running_loop()
    try:
        loop.remove_signal_handler(signal.SIGINT)
    except (NotImplementedError, RuntimeError):
        pass

    renderer.ensure_newline()
    t = ev.get("type")
    if t == "plan_exit_request":
        sys.stdout.write(_yellow("⚠ 请求退出 plan 模式，切换到 build 模式？\n"))
    elif t == "permission_request":
        sys.stdout.write(_yellow(f"⚠ {ev.get('message', '')}\n"))
        sys.stdout.write(_dim(f"   path: {ev.get('path', '')}   tool: {ev.get('tool', '')}\n"))
    else:  # subagent_permission_request
        sys.stdout.write(_yellow(f"⚠ {ev.get('message', '')}\n"))
        sys.stdout.write(
            _dim(
                f"   subagent: {ev.get('child_description', '')}"
                f"   path: {ev.get('path', '')}   tool: {ev.get('tool', '')}\n"
            )
        )

    result = "continue"
    try:
        line = input(_rl_wrap(_yellow("允许? [y/N] ")))
    except EOFError:  # Ctrl+D: deny, then exit
        sys.stdout.write("\n")
        sys.stdout.flush()
        renderer.at_line_start = True
        _resolve(ev, agent, False)
        result = "exit"
    except KeyboardInterrupt:  # Ctrl+C: cancel the run, drop back to IDLE
        sys.stdout.write("\n")
        sys.stdout.flush()
        renderer.at_line_start = True
        agent.cancel()  # the permission wait breaks via CancelledError
    else:
        approved = line.strip().lower() in ("y", "yes")
        # Enter already moved the cursor to a new line.
        renderer.at_line_start = True
        _resolve(ev, agent, approved)
    finally:
        try:
            loop.add_signal_handler(signal.SIGINT, agent.cancel)
        except (NotImplementedError, RuntimeError):
            pass
    return result


# --- REPL states -------------------------------------------------------------

async def _running(agent: Agent, queue: asyncio.Queue, renderer: Renderer) -> bool:
    """Consume broker events until the run ends.

    SIGINT cancels the run via a loop signal handler and surfaces as a
    ``cancelled`` terminal event. There is no steering and no stdin read while
    running -- to interrupt, press Ctrl+C. Returns True if the user asked to
    exit (EOF on an interactive prompt).
    """
    loop = asyncio.get_running_loop()
    try:
        loop.add_signal_handler(signal.SIGINT, agent.cancel)
    except (NotImplementedError, RuntimeError):
        pass

    should_exit = False
    try:
        while True:
            ev = await queue.get()
            t = ev.get("type")
            if t in _INTERACTIVE:
                if await _handle_interactive(agent, ev, renderer) == "exit":
                    agent.cancel()
                    should_exit = True
            else:
                renderer.render(ev)
                if t in _TERMINAL:
                    return should_exit
    finally:
        try:
            loop.remove_signal_handler(signal.SIGINT)
        except (NotImplementedError, RuntimeError):
            pass
    return should_exit


# --- entry point -------------------------------------------------------------

def _write_session_json(session_dir: Path, info: dict) -> None:
    (session_dir / "session.json").write_text(json.dumps(info, indent=2, ensure_ascii=False))


def _create_session(working_dir: str) -> tuple[Path, dict]:
    """Create a new session dir + session.json, mirroring POST /api/sessions."""
    working_dir = str(Path(working_dir).resolve())
    name = os.path.basename(working_dir) or "cli"
    session_dir = get_sessions_dir() / uuid.uuid4().hex
    session_dir.mkdir(parents=True, exist_ok=True)
    now = time.time()
    info = {
        "name": name,
        "working_dir": working_dir,
        "plan_mode": False,
        "additional_dirs": [],
        "created_at": now,
    }
    _write_session_json(session_dir, info)
    return session_dir, info


def _find_latest_session(cwd: Path) -> tuple[Path | None, dict | None]:
    """Most recently saved *main* session whose working_dir resolves to ``cwd``.

    "Latest" is judged by ``history.json``'s mtime (the last time history was
    saved), so activity in either web or CLI is reflected automatically.
    Subagent sessions (``subagent_type`` set) are skipped; only top-level
    sessions are continuable from the CLI.
    """
    target = cwd.resolve()
    best_dir: Path | None = None
    best_info: dict | None = None
    best_ts = -1.0
    for d in get_sessions_dir().iterdir():
        sj = d / "session.json"
        if not sj.is_file():
            continue
        try:
            info = json.loads(sj.read_text())
        except (json.JSONDecodeError, OSError):
            continue
        if info.get("subagent_type"):  # skip subagent sessions
            continue
        wd = info.get("working_dir")
        # Only absolute paths can be reliably matched to a cwd; a relative
        # working_dir would resolve against the *current* cwd and match wrongly.
        if not wd or not Path(wd).is_absolute():
            continue
        try:
            if Path(wd).resolve() != target:
                continue
        except OSError:
            continue
        # Last save time; fall back to created_at for never-messaged sessions.
        hist = d / "history.json"
        try:
            ts = hist.stat().st_mtime if hist.is_file() else (info.get("created_at") or 0)
        except OSError:
            ts = info.get("created_at") or 0
        if ts > best_ts:
            best_ts = ts
            best_dir = d
            best_info = info
    return best_dir, best_info


async def _make_agent(
    config: dict, session_dir: Path, info: dict, *, resume: bool
) -> tuple[Agent, asyncio.Queue]:
    """Instantiate an Agent over a session dir and subscribe to its broker.

    On resume the Agent loads the existing history.json; ``session_id`` is
    reused and persisted plan_mode / additional_dirs are restored.
    """
    registry: dict[str, Agent] = {}
    agent = Agent(
        name=session_dir.name,
        working_dir=info["working_dir"],
        session_dir=session_dir,
        config=config,
        session_id=info.get("session_id"),
        registry=registry,
    )
    registry[agent.name] = agent
    if resume:
        if info.get("additional_dirs"):
            agent.set_additional_dirs(info["additional_dirs"])
        if info.get("plan_mode"):
            agent.set_plan_mode(info["plan_mode"])
    queue, _snapshot = await agent.attach_subscriber()
    return agent, queue


async def run_cli(working_dir: str | None, resume: bool) -> None:
    config = init_config()

    if resume:
        cwd = Path.cwd()
        session_dir, info = _find_latest_session(cwd)
        if session_dir is None:
            sys.stdout.write(_dim(f"# {cwd} 无历史 session，新建\n"))
            sys.stdout.flush()
            session_dir, info = _create_session(str(cwd))
            agent, queue = await _make_agent(config, session_dir, info, resume=False)
            info["session_id"] = agent.session_id
            _write_session_json(session_dir, info)
            banner = f"# trilobite cli · {info['working_dir']}"
        else:
            agent, queue = await _make_agent(config, session_dir, info, resume=True)
            banner = f"# resumed · {info.get('name', session_dir.name)} · {info.get('working_dir', cwd)}"
    else:
        session_dir, info = _create_session(working_dir)
        agent, queue = await _make_agent(config, session_dir, info, resume=False)
        info["session_id"] = agent.session_id
        _write_session_json(session_dir, info)
        banner = f"# trilobite cli · {info['working_dir']}"

    await _repl(agent, queue, banner)


async def _repl(agent: Agent, queue: asyncio.Queue, banner: str) -> None:
    # asyncio.run installs a SIGINT handler that cancels the main task -- useless
    # while blocked in a synchronous input(), since the cancel can't land until
    # input() returns and Ctrl+C is simply swallowed. Switch to Python's default
    # handler so Ctrl+C raises KeyboardInterrupt out of input(). RUNNING
    # temporarily installs a loop-level handler (agent.cancel) and restores this
    # default on exit.
    signal.signal(signal.SIGINT, signal.default_int_handler)

    renderer = Renderer()
    sys.stdout.write(_dim(f"{banner} · Ctrl+C 中断 / /exit 退出\n"))
    sys.stdout.flush()
    renderer.at_line_start = True

    try:
        while True:
            # --- IDLE ---
            renderer.ensure_newline()
            try:
                line = input(_rl_wrap(_blue("❯ ")))
            except (KeyboardInterrupt, EOFError):
                if _COLOR:
                    sys.stdout.write("\n")
                    sys.stdout.flush()
                break
            renderer.at_line_start = True  # Enter moved to a new line
            stripped = line.strip()
            if stripped == "":
                continue
            if stripped in ("/exit", "/quit"):
                break
            if stripped == "/stop":
                sys.stdout.write(_dim("（未运行）\n"))
                sys.stdout.flush()
                renderer.at_line_start = True
                continue

            # Re-render the human input in blue (IDLE has no concurrent output,
            # so readline's echo can be safely overwritten). Single-line input.
            if _COLOR:
                sys.stdout.write("\033[A\033[2K" + _blue(f"❯ {line}") + "\n")
            else:
                sys.stdout.write(f"❯ {line}\n")
            sys.stdout.flush()
            renderer.at_line_start = True

            # --- RUNNING ---
            await agent.start(line)
            if await _running(agent, queue, renderer):
                break
    finally:
        agent.detach_subscriber(queue)
        await agent.aclose()
