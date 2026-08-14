from __future__ import annotations

import asyncio
import json
import logging
import os
import platform
import subprocess
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

import httpx
from typing import Any

from src.trilobite.broker import StreamBroker
from src.trilobite.compaction import should_compact, build_compact_prompt
from src.trilobite.config import DEFAULT_MAX_CONTEXT_TOKENS, DEFAULT_MAX_TOKENS
from src.trilobite.file_access import normalize_dir
from src.trilobite.history import History
from src.trilobite.messages import (
    AssistantMessage,
    CompactMarker,
    Image,
    SystemMessage,
    ToolCall,
    ToolResult,
    UserMessage,
)
from src.trilobite.prompts import (
    CRON_ROLE_PROMPT,
    IMAGE_READ_PROMPT,
    SUBAGENT_ROLE_PREFIX,
    SYSTEM_PROMPT,
    subagent_system_prompt,
)
from src.trilobite.permission import (
    AgentPermission,
    BuildModePermission,
    CronSubagentPermission,
    ExploreSubagentPermission,
    GeneralSubagentPermission,
    PlanModePermission,
)
from src.trilobite.skills import discover_skills, format_skill_listing
from src.trilobite.tools.bash import kill_process_group, truncate_output
from src.trilobite.tool_call import execute_tool

_CHARS = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"

# ── opencode-compatible session ID ──────────────────────────────────────────

def _generate_session_id() -> str:
    """Generate an opencode-style session ID: ses_ + 26 chars."""
    ts_ms = int(time.time() * 1000)
    current = ts_ms << 12  # * 4096, counter 0 is fine
    value = ~current & 0xFFFFFFFFFFFFFFFF  # 64-bit NOT, descending
    time_hex = "".join(f"{(value >> (40 - 8 * i)) & 0xFF:02x}" for i in range(6))
    rand = "".join(_CHARS[b % 62] for b in os.urandom(14))
    return f"ses_{time_hex}{rand}"


# ── thin SSE-chunk wrappers (mirror OpenAI SDK shapes) ──────────────────────

class CronBoundaryError(Exception):
    """A scheduled agent's fire was aborted for an out-of-workspace access.

    Raised inside the run loop after the denial text is recorded as the tool
    result; the run's error handler turns it into a terminal ``error`` state
    (the schedule itself survives and fires again next time).
    """

@dataclass
class _Function:
    name: str = ""
    arguments: str = ""

@dataclass
class _ToolCall:
    id: str = ""
    function: _Function = field(default_factory=_Function)

@dataclass
class _Delta:
    content: str | None = None
    reasoning_content: str | None = None
    tool_calls: list[_ToolCall] | None = None

@dataclass
class _Choice:
    delta: _Delta = field(default_factory=_Delta)
    finish_reason: str | None = None

@dataclass
class _Usage:
    total_tokens: int = 0

@dataclass
class _StreamChunk:
    choices: list[_Choice] = field(default_factory=list)
    usage: _Usage | None = None

PLAN_MODE_NOTIFICATION = (
    '<modeswitch mode="plan">\n'
    "You are now in plan mode (read-only analysis).\n"
    "The following tools are blocked and will be rejected if called: edit, write.\n"
    "All other tools remain available: read, glob, grep, bash, TodoList, exit_plan_mode, task.\n"
    "Note: in plan mode the task tool may only spawn explore (read-only) subagents.\n"
    "Focus on exploring, analyzing, and planning. To make file changes, call exit_plan_mode to request switching to build mode.\n"
    "</modeswitch>"
)

BUILD_MODE_NOTIFICATION = (
    '<modeswitch mode="build">\n'
    "You are now in build mode (full access).\n"
    "All tools are available: read, glob, grep, edit, write, bash, TodoList, task.\n"
    "(exit_plan_mode is a no-op in build mode and will be rejected if called.)\n"
    "You may make file changes, run shell commands, and use your full arsenal of tools.\n"
    "</modeswitch>"
)


# ── httpx-based OpenAI-compatible streaming chat completions ────────────────

async def _chat_completion_stream(
    http: httpx.AsyncClient,
    api_key: str,
    body: dict,
    log: logging.Logger | None = None,
):
    """Stream SSE chunks from an OpenAI-compatible chat/completions endpoint."""
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "Accept": "text/event-stream",
    }
    if log:
        log.info("STREAM request model=%s messages=%d tools=%d reasoning=%s max_tokens=%s",
                 body.get("model"), len(body.get("messages", [])),
                 len(body.get("tools", []) or []), body.get("reasoning_effort"), body.get("max_tokens"))
    chunk_count = 0
    finish_reasons: list[str | None] = []
    try:
        async with http.stream(
            "POST",
            "/chat/completions",
            json=body,
            headers=headers,
        ) as response:
            if log:
                log.info("STREAM response status=%s content-type=%s",
                         response.status_code, response.headers.get("content-type"))
            response.raise_for_status()
            done_seen = False
            async for line in response.aiter_lines():
                if log:
                    log.debug("STREAM raw> %s", line[:800])
                if not line.startswith("data: "):
                    continue
                data_str = line[6:]
                if data_str == "[DONE]":
                    done_seen = True
                    if log:
                        log.info("STREAM [DONE] after %d chunks, finish_reasons=%s",
                                 chunk_count, finish_reasons)
                    break
                try:
                    data = json.loads(data_str)
                    choices = []
                    for c in data.get("choices", []):
                        d = c.get("delta", {})
                        tc_list = []
                        # Some providers (e.g. GLM via opencode zen) emit
                        # ``"tool_calls": null`` on every delta chunk. Unlike a
                        # missing key, ``dict.get(..., [])`` returns ``None``
                        # here (key present, value null) and iterating it
                        # raises TypeError. ``... or []`` covers both cases.
                        for t in (d.get("tool_calls") or []):
                            tc_list.append(_ToolCall(
                                id=t.get("id", ""),
                                function=_Function(
                                    name=t.get("function", {}).get("name", ""),
                                    arguments=t.get("function", {}).get("arguments", ""),
                                ),
                            ))
                        fr = c.get("finish_reason")
                        choices.append(_Choice(
                            delta=_Delta(
                                content=d.get("content"),
                                reasoning_content=d.get("reasoning_content"),
                                tool_calls=tc_list if tc_list else None,
                            ),
                            finish_reason=fr,
                        ))
                    usage_raw = data.get("usage")
                    usage = _Usage(total_tokens=usage_raw.get("total_tokens", 0)) if usage_raw else None
                    chunk_count += 1
                    if log:
                        for idx, ch in enumerate(choices):
                            dlt = ch.delta
                            log.debug("STREAM chunk#%d c[%d] finish=%s content=%d reasoning=%d tc=%d",
                                      chunk_count, idx, ch.finish_reason,
                                      len(dlt.content) if dlt.content else 0,
                                      len(dlt.reasoning_content) if dlt.reasoning_content else 0,
                                      len(dlt.tool_calls) if dlt.tool_calls else 0)
                            if ch.finish_reason:
                                finish_reasons.append(ch.finish_reason)
                    yield _StreamChunk(choices=choices, usage=usage)
                except json.JSONDecodeError:
                    if log:
                        log.warning("STREAM json decode error: %s", data_str[:200])
                    continue
            if not done_seen:
                if log:
                    log.warning("STREAM ended WITHOUT [DONE] after %d chunks, finish_reasons=%s",
                                chunk_count, finish_reasons)
    except Exception as e:
        if log:
            log.exception("STREAM error: %r", e)
        raise


class Agent:
    def __init__(
        self,
        name: str,
        working_dir: str,
        session_dir: Path,
        config: dict[str, str],
        session_id: str | None = None,
        *,
        subagent_type: str | None = None,
        description: str | None = None,
        registry: dict[str, Agent] | None = None,
        parent: Agent | None = None,
        depth: int = 0,
        max_steps: int | None = None,
        sealed: bool = False,
        scheduled: bool = False,
        scheduled_allow_dirs: list[str] | None = None,
        cron_service: Any = None,
    ):
        self.name = name
        self.working_dir = Path(working_dir).resolve()
        self.session_dir = session_dir
        self.config = config
        self._session_id = session_id or _generate_session_id()
        self._api_url = config["api_url"].rstrip("/")
        self._http = httpx.AsyncClient(
            base_url=self._api_url,
            headers={
                "User-Agent": "opencode/1.18.4",
                "x-session-affinity": self._session_id,
                "X-Session-Id": self._session_id,
            },
            timeout=httpx.Timeout(600, connect=10),
        )
        self.model = config["model"]
        self._log = logging.getLogger(f"trilobite.agent.{name}")
        if not self._log.handlers:
            _fh = logging.FileHandler(self.session_dir / "agent.log", encoding="utf-8")
            _fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
            self._log.addHandler(_fh)
        self._log.setLevel(config.get("log_level", "WARNING"))
        self._log.propagate = False
        self.reasoning_effort = config.get("reasoning_effort", "max")
        self.max_context_tokens = int(config.get("max_context_tokens", DEFAULT_MAX_CONTEXT_TOKENS))
        self.max_tokens = int(config.get("max_tokens", DEFAULT_MAX_TOKENS))
        self.compaction_trigger_ratio = float(config.get("compaction_trigger_ratio", 0.7))
        self.system_prompt = SYSTEM_PROMPT
        # Subagents override the system prompt with the role prefix + guidance
        # and use a fixed role permission (never plan/build mode).
        self._subagent_type: str | None = subagent_type
        self._description: str = description or ""
        # A scheduled agent is a role too (general), but with an unattended
        # lifecycle: each cron fire reuses the same session and the agent is
        # never sealed. It is not a ``subagent_type`` so is_sealed()/is_subagent
        # semantics stay intact; ``kind`` is what the frontend keys on.
        self._scheduled: bool = scheduled
        if subagent_type == "explore":
            self.system_prompt = subagent_system_prompt("explore")
        elif subagent_type == "general":
            self.system_prompt = subagent_system_prompt("general")
        elif scheduled:
            # Scheduled agents: base system prompt + the shared subagent
            # prefix + a cron role prompt that injects the allowed directory
            # list (workspace + the owner's additional_dirs at creation) and
            # warns that out-of-workspace access aborts the run.
            allow_dirs = "\n".join(f"- {d}" for d in (scheduled_allow_dirs or [])) or "(none)"
            self.system_prompt = (
                SYSTEM_PROMPT + "\n\n" + SUBAGENT_ROLE_PREFIX + "\n\n"
                + CRON_ROLE_PROMPT.format(allow_dirs=allow_dirs)
            )
        # Prepend a dynamic environment block (working dir, git, platform)
        # so the model knows where it is and prefers relative paths over
        # guessed absolute paths that drift outside the workspace.
        self._is_git_repo = self._detect_git_repo()
        self.system_prompt = self._build_env_block() + "\n\n" + self.system_prompt
        if self.config.get("enable_vl", False):
            self.system_prompt += "\n\n" + IMAGE_READ_PROMPT
        # Append the available-skills listing (name/description/path only;
        # the full body loads on demand via the skill tool). Like the env
        # block and AGENTS.md, the listing is baked into the system message
        # at session start and re-generated on compaction.
        listing = format_skill_listing(
            discover_skills(self.working_dir, self.config.get("skill_dirs", []))
        )
        if listing:
            self.system_prompt += "\n\n" + listing
        self.working_context = self._load_working_context()
        self.history = History(session_dir / "history.json")
        self._broker = StreamBroker(len(self.history.raw))
        self._token_count: int = 0
        self._token_covered: int = 0
        self._task: asyncio.Task | None = None
        # Continuation signals for the run loop. The loop runs another turn if
        # either (a) the previous turn produced tool_calls whose results the
        # model has not seen yet, or (b) a user message (start/steer) arrived
        # since the model last read history. ``_force_run`` is set after
        # compaction so the model gets one turn on the rebuilt context.
        # ``_need_compact`` requests the next turn produce a handoff summary;
        # tools are still advertised (for cache stability) but any call is
        # intercepted. The turn after that rebuilds the context.
        self._pending_tool_results: bool = False
        self._user_read_cursor: int = 0
        self._force_run: bool = False
        self._need_compact: bool = False
        if subagent_type == "explore":
            self._permission: AgentPermission = ExploreSubagentPermission()
        elif subagent_type == "general":
            self._permission: AgentPermission = GeneralSubagentPermission()
        elif scheduled:
            self._permission: AgentPermission = CronSubagentPermission()
        else:
            self._permission: AgentPermission = BuildModePermission()
        self._last_notified_mode: bool | None = None
        self._additional_dirs: list[Path] = []
        self._plan_exit_event: asyncio.Event = asyncio.Event()
        self._plan_exit_approved: bool = False
        self._permission_event: asyncio.Event = asyncio.Event()
        self._permission_approved: bool = False
        self._permission_path: str = ""
        # ── subagent lifecycle ───────────────────────────────────────────
        self._registry: dict[str, Agent] | None = registry
        self._parent: Agent | None = parent
        self._depth: int = depth
        self._max_steps: int | None = max_steps
        self._sealed: bool = sealed
        self._interrupted: bool = False
        self._step_count: int = 0
        self._children: list[Agent] = []
        # The Popen of the bash command currently running in a worker thread
        # (None when idle). Set/cleared via _register_proc so interrupt() can
        # kill it instead of blocking until the command finishes on its own.
        self._current_proc = None
        # The event loop of the run coroutine, captured so worker-thread
        # callbacks (bash on_output) can schedule stream events back onto it
        # via asyncio.run_coroutine_threadsafe.
        self._loop = None
        # Per-tool-call buffer of streamed bash output lines (text, stream).
        # When a run is cancelled mid-command, the asyncio.to_thread future is
        # cancelled and execute_tool's return value is lost; this buffer lets
        # the CancelledError handler salvage the partial output the on_output
        # callback already collected. Keyed by tool_call_id.
        self._tool_output_buffer: dict[str, list[tuple[str, str]]] = {}
        self._initial_prompt: str = ""
        self._final_state: str = "completed"
        self._final_result: str = ""
        # The CronService backing the cron_* virtual tools (injected by the
        # server for primary agents; None for CLI/subagents -> tools error).
        self._cron_service = cron_service
        #: Set when a scheduled agent's tool call goes out of the allowed
        #: directories: the denial is recorded as the tool result, then the
        #: run aborts with a CronBoundaryError (state=error).
        self._cron_boundary_error: str | None = None

    @property
    def session_id(self) -> str:
        return self._session_id

    async def chat_completion(self, messages: list[dict], stream: bool = False, tools: list[dict] | None = None):
        """Make a chat completion request. Returns parsed JSON for non-stream,
        or an async generator of _StreamChunk for stream."""
        body: dict = {"model": self.model, "messages": messages}
        if tools is not None:
            body["tools"] = tools
        body["stream"] = stream
        body["max_tokens"] = self.max_tokens
        if stream:
            body["stream_options"] = {"include_usage": True}
            if self.reasoning_effort:
                body["reasoning_effort"] = self.reasoning_effort
                body["thinking"] = {"type": "enabled"}
            return _chat_completion_stream(self._http, self.config["api_key"], body, log=self._log)
        else:
            headers = {
                "Authorization": f"Bearer {self.config['api_key']}",
                "Content-Type": "application/json",
            }
            resp = await self._http.post("/chat/completions", json=body, headers=headers)
            resp.raise_for_status()
            return resp.json()

    def _load_working_context(self) -> str:
        """Load AGENTS.md and other context from the working directory."""
        agents_md = self.working_dir / "AGENTS.md"
        if agents_md.exists():
            try:
                content = agents_md.read_text(encoding="utf-8", errors="replace")
                return f"\n\n<AGENTS.md>\n{content}\n</AGENTS.md>"
            except Exception:
                return ""
        return ""

    def _detect_git_repo(self) -> bool:
        """True when the working directory is inside a git work tree."""
        try:
            result = subprocess.run(
                ["git", "-C", str(self.working_dir), "rev-parse", "--is-inside-work-tree"],
                capture_output=True,
                text=True,
                timeout=3,
            )
            return result.returncode == 0 and result.stdout.strip() == "true"
        except Exception:
            return False

    def _build_env_block(self) -> str:
        """Build the dynamic environment block prepended to the system prompt.

        Mirrors opencode's ``<env>`` block: gives the model its concrete
        working directory and surroundings so it works with relative paths
        instead of guessing absolute paths that land outside the workspace.
        """
        lines = [
            "<env>",
            f"  Working directory: {self.working_dir}",
            f"  Is directory a git repo: {'yes' if self._is_git_repo else 'no'}",
            f"  Platform: {platform.system().lower()}",
            "</env>",
        ]
        return "\n".join(lines)

    def _ensure_system_message(self):
        """Ensure history starts with a system message.

        For new sessions or old histories without one, create it from current
        config. Once recorded, the system message is immutable in history.
        """
        if not self.history or not isinstance(self.history[0], SystemMessage):
            self.history.insert(0, SystemMessage(self.system_prompt + self.working_context))

    @property
    def _plan_mode(self) -> bool:
        """True when the primary agent is running in plan mode.

        Derived from the active permission policy rather than stored as a
        separate flag, so the permission is the single source of truth.
        """
        return isinstance(self._permission, PlanModePermission)

    def set_plan_mode(self, mode: bool) -> None:
        """Switch the primary agent between plan and build mode.

        This swaps the permission policy in place -- a mode change on a
        running agent, not a new agent definition. The notification logic
        in ``run`` notices the swap and tells the model. Subagents are fixed
        roles and ignore this.
        """
        if self._subagent_type is not None or self._scheduled:
            return
        self._permission = PlanModePermission() if mode else BuildModePermission()

    def is_sealed(self) -> bool:
        """True for a subagent whose run has ended (view-only, no new input)."""
        return self._sealed

    def is_scheduled(self) -> bool:
        """True for a scheduled (cron) agent: unattended, never sealed."""
        return self._scheduled

    @property
    def kind(self) -> str:
        """Session kind for the frontend: 'main' | 'subagent' | 'scheduled'."""
        if self._scheduled:
            return "scheduled"
        if self._subagent_type is not None:
            return "subagent"
        return "main"

    def interrupt(self) -> None:
        """Hard-stop a running subagent's current work, then summarize.

        Immediately cancels the in-progress run -- this interrupts an LLM
        stream (the ``async for chunk in stream`` await) or a bash call right
        away, instead of waiting for the run loop to reach the next safe
        point. The run's ``CancelledError`` handler sees the interrupt flag
        and, rather than a bare cancel, runs one tool-less summary turn and
        exits. Also kills a running bash process group and unblocks a pending
        permission wait so nothing is left stuck.
        """
        self._interrupted = True
        self._permission_approved = False
        self._permission_event.set()
        self._kill_current_proc()
        if self._task is not None and not self._task.done():
            self._task.cancel()

    def _register_proc(self, proc) -> None:
        """Record the bash subprocess currently running (or None when idle).

        Called from the worker thread that runs execute_tool; interrupt()
        reads this from the event loop thread. Reference assignment is atomic
        under the GIL, and Popen.kill()/poll() are safe to call cross-thread.
        """
        self._current_proc = proc

    def _kill_current_proc(self) -> None:
        """Kill a running bash process group so a cancelled/interrupted run
        does not leave an orphaned command behind.

        ``cancel()``/``stop()``/``interrupt()`` all cancel the run task, which
        raises CancelledError at the ``await asyncio.to_thread`` and orphans
        the worker thread; without killing the process group the command would
        keep running in the background. Reads ``_current_proc`` (set by the
        worker thread via ``_register_proc``).
        """
        proc = self._current_proc
        if proc is not None and proc.poll() is None:
            kill_process_group(proc)

    def _make_output_callback(self, tool_call_id: str):
        """Build a line-by-line output callback for a bash tool call.

        Runs in the worker thread that executes bash; each output line is
        forwarded to the event loop as a ``tool_output`` stream event keyed by
        ``tool_call_id`` so the frontend can append it to the matching tool.
        Lines are also accumulated in ``_tool_output_buffer`` so a cancelled
        run can salvage the partial output (the cancelled execute_tool never
        returns its result).
        """
        self._tool_output_buffer.pop(tool_call_id, None)  # fresh start
        def _on_output(text: str, stream: str) -> None:
            self._tool_output_buffer.setdefault(tool_call_id, []).append((text, stream))
            if self._loop is None:
                return
            event = {
                "type": "tool_output",
                "tool_call_id": tool_call_id,
                "stream": stream,
                "text": text,
            }
            asyncio.run_coroutine_threadsafe(
                self._send_stream_event(event), self._loop
            )
        return _on_output

    def set_additional_dirs(self, dirs: list[str]):
        """Replace the allowed-directory grants, canonicalized and deduped.

        Grants are normalized (``~`` expanded, symlinks resolved, relative
        paths against the working dir) so the same directory can never be
        listed twice under different spellings. When this session's grants
        change, running subagents are kept in sync: they inherit the new
        grants (so they stop re-requesting what the user already allowed for
        the group) and lose the ones this session dropped.
        """
        old = self._additional_dirs
        new: list[Path] = []
        for d in dirs:
            resolved = normalize_dir(d, self.working_dir)
            if resolved not in new:
                new.append(resolved)
        self._additional_dirs = new
        if self._children and new != old:
            removed = [d for d in old if d not in new]
            for c in list(self._children):
                # Keep the child's own grants, drop what the parent removed,
                # then add the parent's current grants (union semantics).
                merged = [d for d in c._additional_dirs if d not in removed]
                for d in new:
                    if d not in merged:
                        merged.append(d)
                c.set_additional_dirs([str(d) for d in merged])

    def _add_additional_dir(self, path: str):
        """Grant one more directory, persist it, and share it with children."""
        self.set_additional_dirs([str(d) for d in self._additional_dirs] + [path])
        self._persist_additional_dirs()

    def resolve_plan_exit(self, approved: bool):
        self._plan_exit_approved = approved
        self._plan_exit_event.set()

    def resolve_permission(self, approved: bool):
        self._permission_approved = approved
        self._permission_event.set()

    def _persist_additional_dirs(self):
        """Write additional_dirs back to session.json."""
        self._update_session_json({"additional_dirs": [str(d) for d in self._additional_dirs]})

    def _persist_token_count(self):
        """Write token_count back to session.json."""
        self._update_session_json({"token_count": self._token_count})

    def _update_session_json(self, updates: dict):
        """Update fields in session.json."""
        session_json = self.session_dir / "session.json"
        if session_json.exists():
            try:
                info = json.loads(session_json.read_text())
                info.update(updates)
                session_json.write_text(json.dumps(info, indent=2))
            except Exception:
                pass

    def _maybe_auto_title(self, message: str) -> None:
        """Auto-name the session from the first user message.

        Takes the first 50 characters of the message (whitespace collapsed to
        single spaces) as the session ``name``. Skipped once a title has been
        set -- whether by this auto-naming or by a manual rename -- so re-runs
        (e.g. reverting back to the first message) and user-chosen names are
        preserved. The ``titled`` flag in session.json records this.
        """
        session_json = self.session_dir / "session.json"
        if not session_json.exists():
            return
        try:
            info = json.loads(session_json.read_text())
        except Exception:
            return
        if info.get("titled"):
            return
        title = " ".join(message.split())[:50]
        if not title:
            return
        info["name"] = title
        info["titled"] = True
        session_json.write_text(json.dumps(info, indent=2))

    async def _send_stream_event(self, event: dict):
        await self._broker.publish(event, len(self.history.raw))

    def _count_user_messages(self) -> int:
        """Count real user messages, excluding compact summaries and mode notices.

        Compact summaries are stored as :class:`UserMessage` (so the API sees
        them as user content) but are not real user turns, so they must not
        receive a ``user_seq`` - otherwise revert/edit numbering would drift.
        Mode-change notices are the same: persisted as user content for cache
        stability, but not real user turns.
        """
        return sum(
            1 for m in self.history.raw
            if isinstance(m, UserMessage)
            and not m.compact_summary
            and not m.is_mode_notification
        )

    async def _append_image_user_message(self, image: Image) -> None:
        """Insert a follow-up user message carrying an image read by a tool.

        This mirrors opencode: after a tool reads an image, the image is sent as
        a separate ``user`` message (with empty text) so the next assistant turn
        sees it as an image input. The ``<image .../>`` marker in the tool result
        serves as metadata only.
        """
        user_seq = self._count_user_messages()
        self.history.append(UserMessage("", images=[image]))
        await self._send_stream_event({
            "type": "user",
            "text": "",
            "images": [image.to_frontend_dict()],
            "user_seq": user_seq,
        })

    def _has_compactable_content(self) -> bool:
        """Whether there is real conversation after the last compact marker."""
        start = 0
        for i, msg in enumerate(self.history.raw):
            if isinstance(msg, CompactMarker):
                start = i + 1
        for msg in self.history.raw[start:]:
            if isinstance(msg, (UserMessage, AssistantMessage)) and not (
                isinstance(msg, UserMessage)
                and (msg.compact_summary or msg.is_compact_prompt)
            ):
                return True
        return False

    def _collect_pending_steers(self) -> list[str]:
        """Real user messages that arrived after the compact prompt.

        During a compact turn the user may steer; those messages land after the
        compact prompt (and possibly after the in-flight note). They were folded
        into the note's context via ``combine_new_messages`` but are genuine
        user turns the model must still respond to on the fresh context. Left
        behind the compact marker they would be dropped from the API context,
        so the caller re-appends them after the compact summary.
        """
        raw = self.history.raw
        prompt_idx = -1
        for i in range(len(raw) - 1, -1, -1):
            m = raw[i]
            if isinstance(m, UserMessage) and m.is_compact_prompt:
                prompt_idx = i
                break
        if prompt_idx < 0:
            return []
        return [
            m.content for m in raw[prompt_idx + 1:]
            if isinstance(m, UserMessage) and not m.compact_summary and not m.is_compact_prompt
            and not m.is_mode_notification
        ]

    def _finalize_compaction(self, summary: str) -> list[str]:
        """Rebuild the context after a compaction turn.

        Drops a contentless :class:`CompactMarker` (the API-context boundary),
        a fresh :class:`SystemMessage` with the rebuilt prompt, and the summary
        wrapped in ``<compact>`` tags as a ``compact_summary`` user message.
        ``get_api_messages()`` starts just past the marker, so pre-compaction
        messages are dropped from the API context while remaining in persisted
        history.

        Returns the steering user messages that arrived during the compact turn
        so the caller can re-append them (with user events) after the summary;
        otherwise they would sit behind the marker and be lost from the API
        context.
        """
        pending_steers = self._collect_pending_steers()
        rebuilt_system = self.system_prompt + self.working_context
        self.history.append(CompactMarker())
        self.history.append(SystemMessage(rebuilt_system))
        self.history.append(UserMessage(f"<compact>\n{summary}</compact>", compact_summary=True))
        # The pre-marker <modeswitch> notice is now behind the compact marker
        # and dropped from the API context, so re-assert the current mode.
        # Syncing _last_notified_mode prevents a duplicate notice on next run.
        notif = PLAN_MODE_NOTIFICATION if self._plan_mode else BUILD_MODE_NOTIFICATION
        self.history.append(UserMessage(notif, is_mode_notification=True))
        self._last_notified_mode = self._plan_mode
        self._need_compact = False
        self._force_run = True
        self._token_count = 0
        return pending_steers

    async def run(self):
        self._task = asyncio.current_task()
        self._loop = asyncio.get_running_loop()

        # A scheduled agent's instance is reused across fires (its broker
        # keeps the SSE stream alive, so viewers follow each fire live);
        # reset the per-fire run state a previous run may have left behind.
        if self._scheduled:
            self._interrupted = False
            self._step_count = 0
            self._pending_tool_results = False
            self._force_run = False
            self._user_read_cursor = 0

        self._ensure_system_message()
        # Guard against a dangling assistant(tool_calls) lacking results left
        # behind by a crashed/interrupted run -- the API would reject it.
        self._patch_dangling_tool_calls()

        # The current mode is conveyed to the model via a <modeswitch> user
        # message (the tool set is identical across modes for cache stability,
        # so mode awareness cannot come from which tools are listed). On the
        # first run of a session (new or restored) _last_notified_mode is None
        # and the model has not been told its mode yet, so we inject the
        # notice; afterwards we only inject on an actual change. Persisting it
        # keeps the API prefix growing monotonically so the turn stays
        # cacheable. The notice is hidden from the frontend and excluded from
        # user_seq via is_mode_notification. Scheduled agents are a fixed role
        # (no mode), so they never get a notice.
        if not self._scheduled and (
            self._last_notified_mode is None or self._plan_mode != self._last_notified_mode
        ):
            notif = PLAN_MODE_NOTIFICATION if self._plan_mode else BUILD_MODE_NOTIFICATION
            self._last_notified_mode = self._plan_mode
            self.history.append(UserMessage(notif, is_mode_notification=True))

        # The assistant message being streamed/mutated in the current turn
        # (None outside a turn). Held as locals so the CancelledError handler
        # can salvage or discard partial output.
        current_asst: AssistantMessage | None = None
        current_asst_persisted = False

        try:
            while True:
                # ── continuation check ── run another turn only when there is
                # something new for the model to respond to: tool results it
                # has not seen, a user message (start/steer) it has not read,
                # or a forced turn after compaction. Otherwise the run ends.
                has_unread_user = self._count_user_messages() > self._user_read_cursor
                if not (self._pending_tool_results or has_unread_user or self._force_run):
                    break
                # About to run a turn, which consumes any pending tool results
                # and forced run; clear them so a plain-text turn afterwards
                # does not spuriously continue.
                self._force_run = False
                self._pending_tool_results = False

                # Subagent step cap: prevent runaway loops.
                if self._max_steps is not None and self._step_count >= self._max_steps:
                    self._final_state = "error"
                    self._final_result = f"max_steps ({self._max_steps}) exceeded"
                    await self._send_stream_event({"type": "error", "text": self._final_result})
                    break
                messages = self.history.get_api_messages(
                    image_dir=self.session_dir / "images",
                    enable_vl=bool(self.config.get("enable_vl", False)),
                )
                # Record how many user messages the model is reading this turn
                # (at get_api_messages time, before the stream drains). A steer
                # arriving mid-drain lands after this cursor and drives the
                # next continuation check.
                self._user_read_cursor = self._count_user_messages()

                await self._send_stream_event({"type": "turn"})

                # The tools list is identical on every turn and across plan/build
                # mode switches (both modes advertise the full set), so the
                # request prefix stays cache-stable. Mode differences are
                # conveyed via the <modeswitch> notice and enforced by
                # permission.intercept at execution time, not by withholding
                # tool definitions. The compaction turn keeps the same tools too;
                # any tool call it makes is intercepted (see the _need_compact
                # branch below) so the tools never actually execute.
                tools = self._permission.filter_definitions(
                    enable_vl=bool(self.config.get("enable_vl", False))
                )
                stream = await self.chat_completion(
                    messages=messages,
                    stream=True,
                    tools=tools,
                )

                # Begin the turn: append an empty assistant message and mutate
                # it as the stream drains. It is persisted only once finalized,
                # so a crash mid-turn leaves no half-written entry on disk.
                asst = AssistantMessage()
                self.history.append(asst, persist=False)
                current_asst = asst
                current_asst_persisted = False
                # Drop buffered bash output from previous turns -- it is only
                # needed to salvage a cancelled in-flight command, and a new
                # turn means the previous one completed (or was salvaged).
                self._tool_output_buffer.clear()
                current_tool_id = ""
                current_tool_name = ""
                current_tool_args = ""

                async for chunk in stream:
                    delta = chunk.choices[0].delta if chunk.choices else None

                    # Capture real token usage from stream
                    if chunk.usage:
                        self._token_count = chunk.usage.total_tokens
                        self._token_covered = len(self.history)
                        self._persist_token_count()

                    if delta is None:
                        continue

                    if hasattr(delta, "reasoning_content") and delta.reasoning_content:
                        asst.thinking += delta.reasoning_content
                        await self._send_stream_event({"type": "thinking", "text": delta.reasoning_content})

                    if delta.content:
                        asst.content += delta.content
                        await self._send_stream_event({"type": "text", "text": delta.content})

                    if delta.tool_calls:
                        for tc in delta.tool_calls:
                            if tc.id:
                                if current_tool_id:
                                    await self._send_stream_event({
                                        "type": "tool_stream",
                                        "tool_name": current_tool_name,
                                        "args": current_tool_args,
                                        "complete": True,
                                    })
                                    asst.tool_calls.append(ToolCall(
                                        id=current_tool_id,
                                        name=current_tool_name,
                                        arguments=current_tool_args,
                                    ))
                                current_tool_id = tc.id
                                current_tool_name = tc.function.name if tc.function else ""
                                current_tool_args = ""
                                if current_tool_name:
                                    await self._send_stream_event({
                                        "type": "tool_stream",
                                        "tool_name": current_tool_name,
                                        "args": "",
                                        "complete": False,
                                    })
                            if tc.function:
                                if tc.function.name:
                                    current_tool_name = tc.function.name
                                    await self._send_stream_event({
                                        "type": "tool_stream",
                                        "tool_name": current_tool_name,
                                        "args": current_tool_args,
                                        "complete": False,
                                    })
                                if tc.function.arguments:
                                    current_tool_args += tc.function.arguments
                                    await self._send_stream_event({
                                        "type": "tool_stream",
                                        "tool_name": current_tool_name,
                                        "args": current_tool_args,
                                        "complete": False,
                                    })

                if current_tool_id:
                    await self._send_stream_event({
                        "type": "tool_stream",
                        "tool_name": current_tool_name,
                        "args": current_tool_args,
                        "complete": True,
                    })
                    asst.tool_calls.append(ToolCall(
                        id=current_tool_id,
                        name=current_tool_name,
                        arguments=current_tool_args,
                    ))

                await self._send_stream_event({
                    "type": "usage",
                    "token_count": self._token_count,
                    "max_context_tokens": self.max_context_tokens,
                })

                self._log.info(
                    "TURN result content_len=%d thinking_len=%d tool_calls=%d token_count=%d plan_mode=%s",
                    len(asst.content), len(asst.thinking), len(asst.tool_calls), self._token_count, self._plan_mode,
                )
                if not asst.content and not asst.thinking and not asst.tool_calls:
                    self._log.warning("TURN produced EMPTY assistant output (no content/thinking/tool_calls)")

                self._step_count += 1

                if asst.tool_calls:
                    # Persist the assistant message (with tool_calls) before
                    # executing tools, so a crash between tools leaves a
                    # patchable dangling entry rather than nothing.
                    self.history.save()
                    current_asst_persisted = True
                    self._pending_tool_results = True

                    for tc in asst.tool_calls:
                        args = {}
                        try:
                            args = json.loads(tc.arguments)
                        except json.JSONDecodeError:
                            pass

                        tool_name = tc.name
                        await self._send_stream_event({"type": "tool_start", "tool": tool_name, "args": args, "tool_call_id": tc.id})

                        tool_result: dict[str, Any]
                        blocked = self._permission.intercept(tool_name)
                        if blocked is not None:
                            tool_result = {"result": blocked}
                        elif self._need_compact:
                            # Compaction turn: tools stay advertised (for
                            # prefix-cache stability) but must not execute. If
                            # the model calls one despite the compaction prompt,
                            # intercept it so no side effects happen; the turn
                            # is retried below until the model emits a text-only
                            # handoff summary.
                            tool_result = {"result": "Compaction in progress: tool calls are not executed during the compaction turn. Respond with the handoff summary as text only, do not call any tools."}
                        elif tool_name == "exit_plan_mode":
                            if not self._plan_mode:
                                tool_result = {"result": "exit_plan_mode is a no-op in build mode; you are already in build mode."}
                            else:
                                # Fan out to the group so the request is visible
                                # even while the user is browsing a subagent.
                                await self._broadcast_to_group({"type": "plan_exit_request", "session": self.name})
                                await self._plan_exit_event.wait()
                                self._plan_exit_event.clear()
                                if self._plan_exit_approved:
                                    self._permission = BuildModePermission()
                                    self._last_notified_mode = False
                                    tool_result = {"result": "Plan mode exited. You are now in build mode and may make file changes."}
                                else:
                                    tool_result = {"result": "User declined. Continue planning in plan mode."}
                        elif tool_name == "task":
                            tool_result = await self._run_subagents(args)
                        elif tool_name in ("cron_create", "cron_list", "cron_delete"):
                            tool_result = await self._run_cron_tool(tool_name, args)
                        else:
                            # Tools are synchronous (notably bash's subprocess.run
                            # blocks). Run them in a worker thread so a long bash
                            # call doesn't freeze the event loop -- otherwise the
                            # shared loop stalls SSE heartbeats and every other
                            # agent/subagent on it (issue #5).
                            on_output = self._make_output_callback(tc.id)
                            tool_result = await asyncio.to_thread(
                                execute_tool, tool_name, args, self.working_dir,
                                self.session_dir, self._additional_dirs,
                                self.config, self._register_proc, on_output)
                            if "image" in tool_result and self.config.get("enable_vl", False):
                                await self._append_image_user_message(tool_result["image"])

                        # Handle permission request from tool
                        if "permission" in tool_result:
                            perm_path = tool_result["permission"]
                            if self._scheduled:
                                # Unattended scheduled agent: an out-of-workspace
                                # access aborts this fire with an error instead
                                # of prompting (nobody would answer). The denial
                                # text is recorded as the tool result below,
                                # then the run terminates with state=error.
                                self._cron_boundary_error = tool_result["result"]
                            elif self._subagent_type is not None and self._parent is not None:
                                # Subagent: broadcast globally (parent + all
                                # siblings) so the prompt reaches the user
                                # regardless of which session they are viewing.
                                await self._parent._broadcast_subagent_permission(
                                    self, perm_path, tool_name, tool_result["result"]
                                )
                            else:
                                # Main session: fan out to its group too.
                                await self._broadcast_to_group({
                                    "type": "permission_request",
                                    "session": self.name,
                                    "path": perm_path,
                                    "tool": tool_name,
                                    "message": tool_result["result"],
                                })
                            self._permission_event.clear()
                            await self._permission_event.wait()
                            if self._permission_approved:
                                # The frontend also calls addDir (persisting to
                                # session.json); set_additional_dirs dedupes so
                                # the approved path is never listed twice.
                                self._add_additional_dir(perm_path)
                                # Retry the tool with updated additional_dirs
                                on_output = self._make_output_callback(tc.id)
                                tool_result = await asyncio.to_thread(
                                    execute_tool, tool_name, args, self.working_dir,
                                    self.session_dir, self._additional_dirs,
                                    self.config, self._register_proc, on_output)
                                if "image" in tool_result and self.config.get("enable_vl", False):
                                    await self._append_image_user_message(tool_result["image"])
                            # else: keep original error result

                        result_event: dict[str, Any] = {"type": "tool_result", "tool": tool_name, "result": tool_result["result"], "tool_call_id": tc.id}
                        if "diff" in tool_result:
                            result_event["diff"] = tool_result["diff"]
                        await self._send_stream_event(result_event)

                        # The tool result lives inside the assistant message
                        # (self-contained turn), so a steering user message can
                        # never land between the tool_calls and their results.
                        asst.tool_results.append(ToolResult(
                            tool_call_id=tc.id,
                            content=tool_result["result"],
                            diff=tool_result.get("diff"),
                        ))
                        self.history.save()

                        # A scheduled agent that went out of bounds: the denial
                        # is now persisted with the tool call; abort the fire.
                        if self._cron_boundary_error is not None:
                            msg = self._cron_boundary_error
                            self._cron_boundary_error = None
                            raise CronBoundaryError(msg)
                else:
                    self.history.save()
                    current_asst_persisted = True

                # ── compaction handling after the turn ──
                if self._need_compact and asst.tool_calls:
                    # The model called tools during the compaction turn despite
                    # the prompt; they were intercepted above (no side effects).
                    # Do not finalize with an empty/partial summary -- loop
                    # again so the model, now seeing the interception results,
                    # retries with a text-only handoff summary.
                    pass
                elif self._need_compact:
                    # This was the compaction turn and the model produced a
                    # text-only handoff summary. Rebuild the context (marker +
                    # fresh system + compact_summary), then re-append any
                    # steering messages that arrived during the compact turn so
                    # the model responds to them on the fresh context instead
                    # of losing them behind the marker.
                    pending_steers = self._finalize_compaction(asst.content)
                    for s in pending_steers:
                        user_seq = self._count_user_messages()
                        self.history.append(UserMessage(s))
                        await self._send_stream_event({"type": "user", "text": s, "user_seq": user_seq})
                    self._token_covered = len(self.history.raw)
                    self._persist_token_count()
                    await self._broker.commit(len(self.history.raw))
                    await self._send_stream_event({"type": "compact"})
                elif should_compact(self):
                    # Token threshold exceeded: request a compaction turn next.
                    # The compact prompt is a marked user message -- it combines
                    # with any pending steering via combine_new_messages and the
                    # next turn asks for a handoff summary (tools advertised but
                    # intercepted). Steering that arrives during the compact
                    # turn is re-appended past the marker by _finalize_compaction
                    # so it is not lost.
                    self._need_compact = True
                    prompt = build_compact_prompt(self)
                    user_seq = self._count_user_messages()
                    self.history.append(UserMessage(prompt, is_compact_prompt=True))
                    await self._send_stream_event({"type": "user", "text": prompt, "user_seq": user_seq})
                elif not asst.tool_calls:
                    # Plain-text final turn, nothing pending: the run completes.
                    self._final_state = "completed"
                    self._final_result = asst.content

            # Loop exited normally: the run completed with no pending work.
            # Emit ``done`` only for a completed run (max_steps already
            # emitted ``error`` above).
            if self._final_state == "completed":
                await self._send_stream_event({"type": "done"})

        except asyncio.CancelledError:
            if self._interrupted:
                # A subagent interrupt hard-stops the current turn mid-flight
                # (LLM stream or bash). Clear the pending cancellation, keep
                # any partial output (e.g. half-streamed thinking) so the
                # summary turn carries it to the API, salvage partial bash
                # output, then run one summary turn before exiting cleanly --
                # so the parent's task tool gets a summary result instead of a
                # bare cancellation. (A real cancel from the parent leaves
                # _interrupted False and falls through to the hard-stop path.)
                if self._task is not None:
                    self._task.uncancel()
                # Keep partial output from an interrupted in-flight turn
                # (mid-drain, unpersisted) so the summary turn's API call
                # carries the half-streamed thinking -- a bare discard would
                # lose the model's reasoning. Only drop a truly empty
                # placeholder (nothing streamed yet). A persisted turn (mid
                # tool-execution) is already in history.
                if current_asst is not None and not current_asst_persisted:
                    if self.history.raw and self.history.raw[-1] is current_asst:
                        if current_asst.thinking or current_asst.content or current_asst.tool_calls:
                            self.history.save()
                        else:
                            self.history.pop()
                # Salvage partial bash output for the in-flight tool call.
                self._salvage_inflight_tool(current_asst)
                self._patch_dangling_tool_calls()
                try:
                    await self._summarize_and_exit()
                except asyncio.CancelledError:
                    # Parent cancelled us mid-summary: hard-stop, no summary.
                    raise
                except Exception as e:
                    self._final_state = "error"
                    self._final_result = f"interrupt summary failed: {e}"
                    await self._send_stream_event({"type": "error", "text": self._final_result})
                return
            # Hard cancel: salvage partial output from the in-flight turn.
            self._final_state = "error"
            self._final_result = "cancelled"
            # Propagate cancellation to running subagents (hard stop, no summary).
            for c in list(self._children):
                if c._task and not c._task.done():
                    c._task.cancel()
            # Salvage partial bash output for the in-flight tool call before
            # persisting, so the model sees what the cancelled command produced
            # instead of a bare [interrupted] placeholder on the next run.
            self._salvage_inflight_tool(current_asst)
            # Persist the in-flight assistant message if it has any content,
            # thinking, or tool_calls; otherwise drop the empty placeholder so
            # history stays clean.
            if current_asst is not None and self.history.raw and self.history.raw[-1] is current_asst:
                if current_asst.content or current_asst.thinking or current_asst.tool_calls:
                    self.history.save()
                    self._log.warning(
                        "RUN cancelled content_len=%d thinking_len=%d tool_calls=%d",
                        len(current_asst.content), len(current_asst.thinking), len(current_asst.tool_calls))
                else:
                    self.history.pop()
                    self._log.warning("RUN cancelled (no partial output)")
            else:
                self._log.warning("RUN cancelled")
            await self._send_stream_event({"type": "cancelled"})
            raise
        except CronBoundaryError as e:
            # A scheduled fire was aborted for an out-of-workspace access.
            # The denial text is already persisted with the tool call; the
            # schedule itself survives and fires again next time.
            self._final_state = "error"
            self._final_result = str(e)
            await self._send_stream_event({"type": "error", "text": str(e)})
        except Exception as e:
            msg = str(e)
            error_type: str | None = None
            error_code: str | None = None
            status_code: int | None = None

            if hasattr(e, "body") and isinstance(e.body, dict):
                err = e.body.get("error", {})
                msg = err.get("message", msg)
                error_type = err.get("type")
                error_code = err.get("code")

            if hasattr(e, "status_code"):
                status_code = e.status_code

            self._final_state = "error"
            self._final_result = msg
            self._log.exception("RUN error status=%s type=%s code=%s msg=%s",
                                status_code, error_type, error_code, msg)
            await self._send_stream_event({
                "type": "error",
                "text": msg,
                "status_code": status_code,
                "error_type": error_type,
                "error_code": error_code,
            })
        finally:
            # A subagent's run has ended for any reason -> it is now sealed
            # (view-only, no new input). Scheduled agents are never sealed:
            # the next fire reuses the session.
            if self._subagent_type is not None and not self._scheduled:
                self._sealed = True
            # The run is over regardless of how it ended; clear the running
            # flag (a safety net -- done/cancelled/error already set it) and
            # drop the task reference so is_running() is accurate.
            self._broker.set_running(False)
            self._task = None

    async def steer(self, message: str):
        """Append a steering user message mid-run.

        The message goes straight into history (no queue): the run loop is
        alive, so its next continuation check picks it up and runs another turn
        so the model can respond. It lands after the in-flight assistant turn,
        never splitting tool_calls from their results.
        """
        user_seq = self._count_user_messages()
        self.history.append(UserMessage(message))
        await self._send_stream_event({"type": "user", "text": message, "user_seq": user_seq})

    def add_user_message(self, message: str, images: list[Image] | None = None):
        self.history.append(UserMessage(message, images=images))

    def is_running(self) -> bool:
        return self._broker.is_running

    async def start(self, message: str, images: list[Image] | None = None) -> None:
        """Begin a run independently of any HTTP request lifecycle.

        Closing a browser only drops SSE subscribers; the agent keeps running
        because the task created here is not tied to any request. The user
        message is emitted as a stream event so every subscriber (and any
        reconnecting client) renders it consistently.
        """
        user_seq = self._count_user_messages()
        # Auto-title the session from the first user message (first 50 chars).
        # Subagents carry a description instead and are never auto-titled.
        if user_seq == 0 and self._subagent_type is None:
            self._maybe_auto_title(message)
        self.add_user_message(message, images=images)
        self._broker.set_running(True)
        await self._send_stream_event({
            "type": "user",
            "text": message,
            "images": [img.to_frontend_dict() for img in (images or [])] or None,
            "user_seq": user_seq,
        })
        self._task = asyncio.create_task(self.run())

    async def attach_subscriber(self) -> tuple[asyncio.Queue, dict]:
        """Subscribe a client: replay the current run and snapshot history."""
        q, snapshot = await self._broker.attach(
            self.history.raw,
            self._token_count,
            self.max_context_tokens,
            self._plan_mode,
            [str(d) for d in self._additional_dirs],
        )
        # Expand the committed typed messages into the flat v1-style dict list
        # the frontend expects (an AssistantMessage unfolds into its assistant
        # dict followed by its tool-result dicts).
        snapshot["history"] = [
            d for m in snapshot["history"] for d in m.to_frontend_dicts()
        ]
        snapshot["is_subagent"] = self._subagent_type is not None
        snapshot["kind"] = self.kind
        snapshot["sealed"] = self._sealed
        snapshot["subagent_type"] = self._subagent_type
        snapshot["description"] = self._description
        snapshot["enable_vl"] = bool(self.config.get("enable_vl", False))
        return q, snapshot

    def detach_subscriber(self, q: asyncio.Queue) -> None:
        self._broker.detach(q)

    async def aclose(self) -> None:
        """Release async resources (the httpx client).

        Web mode keeps agents alive for the process lifetime, so this is only
        needed by the CLI, which owns a single short-lived agent.
        """
        await self._http.aclose()

    def cancel(self):
        self._kill_current_proc()
        if self._task and not self._task.done():
            self._task.cancel()
        # Propagate cancellation to running subagents (hard stop, no summary).
        for c in list(self._children):
            c.cancel()

    async def stop(self) -> None:
        """Cancel an in-progress run and wait for it to fully finish."""
        self._kill_current_proc()
        task = self._task
        if task and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            except Exception:
                pass
        for c in list(self._children):
            await c.stop()
        self._task = None
        self._broker.set_running(False)

    # ── subagent spawning ────────────────────────────────────────────────

    def _create_child(self, subagent_type: str, description: str, prompt: str) -> Agent:
        """Build a child Agent for one subtask (does not start it)."""
        child_name = uuid.uuid4().hex
        child_dir = self.session_dir.parent / child_name
        child_dir.mkdir(parents=True, exist_ok=True)
        info = {
            "name": child_name,
            "working_dir": str(self.working_dir),
            "parent_session": self.name,
            "subagent_type": subagent_type,
            "description": description,
            "depth": self._depth + 1,
            "additional_dirs": [str(d) for d in self._additional_dirs],
            "created_at": time.time(),
        }
        (child_dir / "session.json").write_text(json.dumps(info, indent=2))
        child = Agent(
            name=child_name,
            working_dir=str(self.working_dir),
            session_dir=child_dir,
            config=self.config,
            subagent_type=subagent_type,
            description=description,
            registry=self._registry,
            parent=self,
            depth=self._depth + 1,
            max_steps=int(self.config.get("subagent_max_steps", 100)),
        )
        child.set_additional_dirs([str(d) for d in self._additional_dirs])
        child.add_user_message(prompt)
        child._initial_prompt = prompt
        if self._registry is not None:
            self._registry[child_name] = child
        return child

    async def _run_as_subagent(self) -> None:
        """Mark running, emit the initial prompt as a user event, then run.

        Each subagent runs as its own asyncio task so it can be steered or
        interrupted/cancelled independently.
        """
        self._broker.set_running(True)
        await self._send_stream_event({"type": "user", "text": self._initial_prompt, "user_seq": 0})
        await self.run()

    # ── scheduled (cron) agents ────────────────────────────────────────────

    async def _run_cron_tool(self, tool_name: str, args: dict) -> dict[str, Any]:
        """Dispatch a cron_* tool call to the CronService.

        The cron tools are virtual: their execution lives in the CronService
        (scheduler.py), which needs the owning session's identity to persist
        schedules. Only build mode advertises them (plan mode does not expose
        them at all, so a read-only session can never create an unattended
        full-permission agent).
        """
        if self._cron_service is None:
            return {"result": "Error: cron tools are not available in this context."}
        result = self._cron_service.handle(
            self.name, tool_name, args, self.working_dir, self._additional_dirs
        )
        return {"result": result}

    async def start_scheduled_fire(self, prompt_text: str) -> None:
        """Open a new fire of this scheduled agent: fresh API context + run.

        A :class:`CompactMarker` is appended before this fire's messages when
        the session already holds earlier runs: ``get_api_messages()`` starts
        just past the last marker, so the API context covers only this fire,
        while the persisted history keeps every earlier run (the marker is
        what the frontend renders as the run boundary divider).
        """
        if self.history.raw:
            self.history.append(CompactMarker())
        self.history.append(SystemMessage(self.system_prompt + self.working_context))
        self.history.append(UserMessage(prompt_text))
        self._broker.set_running(True)
        await self._send_stream_event({
            "type": "user",
            "text": prompt_text,
            "user_seq": self._count_user_messages() - 1,
        })

    async def _run_subagents(self, args: dict) -> dict[str, Any]:
        """Spawn one or more subagents in parallel, gather their results."""
        specs = args.get("tasks") or []
        if not isinstance(specs, list) or not specs:
            return {"result": "Error: task tool requires a non-empty 'tasks' array."}

        children: list[Agent] = []
        errors: list[str] = []
        for spec in specs:
            if not isinstance(spec, dict):
                errors.append("[invalid task spec]")
                continue
            stype = spec.get("subagent_type")
            desc = spec.get("description", "subagent")
            prompt = spec.get("prompt", "")
            if stype not in ("explore", "general"):
                errors.append(f"[{desc}] invalid subagent_type: {stype}")
                continue
            if self._plan_mode and stype == "general":
                errors.append(f"[{desc}] plan mode can only spawn explore (read-only) subagents")
                continue
            if self._depth >= 1:
                errors.append(f"[{desc}] subagent nesting limit reached")
                continue
            children.append(self._create_child(stype, desc, prompt))

        await self._send_stream_event({
            "type": "subagents",
            "parent": self.name,
            "children": [
                {"session": c.name, "type": c._subagent_type, "description": c._description, "state": "running"}
                for c in children
            ],
        })

        async def _run_and_announce(c: Agent) -> None:
            """Run a child subagent, then emit its terminal state immediately.

            Emitting per-child (rather than after ``gather`` returns) lets the
            task bubble update each subagent row as it finishes, instead of all
            at once when the last one completes.
            """
            await c._run_as_subagent()
            await self._send_stream_event(
                {"type": "subagent_state", "session": c.name, "state": c._final_state}
            )

        self._children = children
        tasks = [asyncio.create_task(_run_and_announce(c)) for c in children]
        await asyncio.gather(*tasks, return_exceptions=True)
        self._children = []

        parts: list[str] = []
        for c in children:
            state = c._final_state
            text = c._final_result
            parts.append(
                f'<subagent session="{c.name}" type="{c._subagent_type}" '
                f'description="{c._description}" state="{state}">\n'
                f"<result>{text}</result>\n</subagent>"
            )

        body = "\n".join(parts)
        if errors:
            body = "\n".join(errors) + "\n" + body
        return {"result": f"<task_result>\n{body}\n</task_result>"}

    def _format_partial_bash_output(self, tool_call_id: str, args: dict) -> str | None:
        """Reconstruct the partial output streamed so far for a bash call.

        Mirrors BashTool's output shape (stdout, then a ``[stderr]`` section)
        but from the ``on_output`` buffer accumulated in the worker thread,
        since the cancelled ``execute_tool`` never returns its result. Returns
        None when nothing was streamed.
        """
        buf = self._tool_output_buffer.get(tool_call_id)
        if not buf:
            return None
        snapshot = list(buf)  # copy: reader threads may still append
        out_lines = [text for text, src in snapshot if src == "stdout"]
        err_lines = [text for text, src in snapshot if src == "stderr"]
        output = "\n".join(out_lines)
        if err_lines:
            if output:
                output += "\n"
            output += "[stderr]\n" + "\n".join(err_lines)
        if not output:
            return None
        max_lines = args.get("max_output_lines", 100)
        max_chars = args.get("max_output_chars", 10000)
        return truncate_output(output, max_lines, max_chars)

    def _salvage_inflight_tool(self, asst: AssistantMessage | None) -> None:
        """Fill the in-flight bash tool's result with partial output on cancel.

        When a run is cancelled mid-tool-execution, the executing tool call has
        no result yet. For bash, salvage the output streamed so far (via
        ``on_output``) and append a note that the user cancelled, so the model
        sees what the command produced instead of a bare ``[interrupted]``
        placeholder. Only the first unanswered tool call (the one actually
        executing) is considered; later, not-yet-started calls are left for
        ``_patch_dangling_tool_calls``.
        """
        if asst is None or not asst.tool_calls:
            return
        answered = {tr.tool_call_id for tr in asst.tool_results}
        for tc in asst.tool_calls:
            if tc.id and tc.id not in answered:
                # The first unanswered tool call is the one that was executing.
                if tc.name == "bash":
                    try:
                        args = json.loads(tc.arguments)
                    except (json.JSONDecodeError, TypeError):
                        args = {}
                    partial = self._format_partial_bash_output(tc.id, args)
                    note = "\n[command cancelled by user; output above is partial]"
                    content = (partial + note) if partial else "[command cancelled by user]"
                    asst.tool_results.append(ToolResult(tool_call_id=tc.id, content=content))
                    self._tool_output_buffer.pop(tc.id, None)
                    self.history.save()
                break

    def _patch_dangling_tool_calls(self) -> None:
        """Fill placeholder tool results for tool_calls lacking a result.

        An interrupt or crash can leave a trailing :class:`AssistantMessage`
        with ``tool_calls`` but not all ``tool_results``. OpenAI-compatible
        APIs reject ``tool_calls`` without matching results, which would break
        the next turn. Scan the trailing assistant message and append a
        placeholder result for any unanswered ``tool_call_id``.
        """
        raw = self.history.raw
        if not raw:
            return
        last = raw[-1]
        if not isinstance(last, AssistantMessage) or not last.tool_calls:
            return
        answered = {tr.tool_call_id for tr in last.tool_results}
        patched = False
        for tc in last.tool_calls:
            if tc.id and tc.id not in answered:
                last.tool_results.append(ToolResult(
                    tool_call_id=tc.id,
                    content="[interrupted]",
                ))
                patched = True
        if patched:
            self.history.save()

    async def _summarize_and_exit(self) -> None:
        """Produce a final summary turn after an interrupt, then exit.

        No further tool calls are allowed; one tool-less LLM call yields the
        summary that becomes this subagent's result.
        """
        summary_prompt = "你被中断了。请简明总结你目前的发现/进展，然后停止。"
        self.history.append(UserMessage(summary_prompt))
        await self._send_stream_event({"type": "user", "text": summary_prompt, "user_seq": self._count_user_messages() - 1})
        await self._send_stream_event({"type": "turn"})
        messages = self.history.get_api_messages(
            image_dir=self.session_dir / "images",
            enable_vl=bool(self.config.get("enable_vl", False)),
        )
        stream = await self.chat_completion(messages=messages, stream=True, tools=None)
        asst = AssistantMessage()
        self.history.append(asst, persist=False)
        async for chunk in stream:
            delta = chunk.choices[0].delta if chunk.choices else None
            if delta is None:
                continue
            if getattr(delta, "content", None):
                asst.content += delta.content
                await self._send_stream_event({"type": "text", "text": delta.content})
        if not asst.content:
            asst.content = "[no summary produced]"
        self.history.save()
        self._final_state = "interrupted"
        self._final_result = asst.content
        await self._send_stream_event({"type": "interrupted"})

    async def _broadcast_to_group(self, event: dict) -> None:
        """Send an event to this agent's own stream and, for the main session,
        to all its running children. Used for the main session's own approval
        requests (permission / plan-exit) so the prompt is visible no matter
        which session the user is currently viewing."""
        await self._send_stream_event(event)
        if self._subagent_type is None:
            for c in list(self._children):
                await c._send_stream_event(event)

    async def _broadcast_subagent_permission(self, child: Agent, path: str, tool: str, message: str) -> None:
        """Fan a subagent's permission request to the parent and all running
        sibling subagents, so the prompt is visible no matter which session
        the user is currently viewing."""
        event = {
            "type": "subagent_permission_request",
            "child_session": child.name,
            "child_type": child._subagent_type,
            "child_description": child._description,
            "path": path,
            "tool": tool,
            "message": message,
        }
        await self._send_stream_event(event)
        for c in list(self._children):
            await c._send_stream_event(event)

    async def revert(self, user_seq: int, message: str) -> str:
        """Edit a previously sent user message and rerun from there.

        Two cases:

        * The message has already been read by the model (``user_seq`` below
          the read cursor): stop the run, truncate history to just before that
          message, then start a fresh run with the edited text.
        * The message has not been read yet (a steering message still pending
          in history): swap its text in place without interrupting the run.

        Returns ``"rerun"`` or ``"queued"`` so the client knows whether to
        reconnect (rerun rebuilds from the truncated history) or just apply a
        local text update (queued).
        """
        # Locate the user_seq-th real user message (compact summaries and mode
        # notices excluded, matching _count_user_messages).
        target = -1
        count = 0
        for i, msg in enumerate(self.history.raw):
            if (
                isinstance(msg, UserMessage)
                and not msg.compact_summary
                and not msg.is_mode_notification
            ):
                if count == user_seq:
                    target = i
                    break
                count += 1
        if target < 0:
            raise ValueError("user message not found")

        if user_seq < self._user_read_cursor or not self.is_running():
            # Already read by the model, or no run is alive to consume an
            # in-place edit: rerun from the edited message.
            if self.is_running():
                await self.stop()
            self.history.truncate(target)
            # Truncation removed the messages the cursor counted past; align it
            # to the truncated history so start()'s new message reads as unread
            # and the run loop actually turns. Otherwise (k+1) > old_cursor is
            # false and the run exits immediately without calling the model.
            self._user_read_cursor = self._count_user_messages()
            await self._broker.commit(target)
            await self.start(message)
            return "rerun"

        # Not yet read and a run is live (steering message pending): edit in place.
        self.history.raw[target].content = message
        self.history.save()
        await self._send_stream_event({"type": "user_edit", "user_seq": user_seq, "text": message})
        return "queued"

    async def compact_now(self) -> None:
        """Manually trigger context compaction (the ``/compact`` command).

        Compacts regardless of the token threshold. The compact prompt is
        appended as a normal user message and the run loop handles the rest:
        the next turn asks for a handoff summary (tools advertised but
        intercepted), then the context is rebuilt and the model continues on
        the fresh context. If there is no real conversation to compact (e.g.
        right after a previous
        compact), a status notice is sent instead.
        """
        self._broker.set_running(True)

        if not self._has_compactable_content():
            await self._send_stream_event({"type": "status", "text": "nothing to compact"})
            await self._send_stream_event({
                "type": "usage",
                "token_count": self._token_count,
                "max_context_tokens": self.max_context_tokens,
            })
            await self._send_stream_event({"type": "done"})
            self._broker.set_running(False)
            self._task = None
            return

        self._need_compact = True
        prompt = build_compact_prompt(self)
        user_seq = self._count_user_messages()
        self.history.append(UserMessage(prompt, is_compact_prompt=True))
        await self._send_stream_event({"type": "user", "text": prompt, "user_seq": user_seq})
        self._task = asyncio.create_task(self.run())
