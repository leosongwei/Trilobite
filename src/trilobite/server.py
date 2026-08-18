import asyncio
import base64
import json
import re
import secrets
import time
import uuid
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from src.trilobite.agent import Agent
from src.trilobite.config import (
    DEFAULT_MAX_CONTEXT_TOKENS,
    get_config_dir,
    get_default_model_name,
    get_model,
    get_sessions_dir,
    init_config,
    load_models,
)
from src.trilobite.file_access import detect_line_ending, materialize, normalize_dir, resolve_file_path
from src.trilobite.git_ops import MAX_DIFF_ROWS, build_diff_rows, list_dir, show_base_content
from src.trilobite.image_storage import ext_to_mime, save_image
from src.trilobite.messages import Image
from src.trilobite.projects import create_project as projects_create, delete_project as projects_delete, load_projects
from src.trilobite.scheduler import CronService, Schedule
from src.trilobite.version import get_version as get_pkg_version

app = FastAPI(title="Trilobite")

agents: dict[str, Agent] = {}
config: dict = {}
cron_service: CronService | None = None

#: Max file size the file manager will read/diff/save (bytes). Larger files
#: are refused and the agent's read tool (paged) is the suggested fallback.
MAX_FILE_SIZE = 512 * 1024

# ── token auth ──────────────────────────────────────────────────────────────
# The web server is guarded by an access token (like Jupyter Notebook). The
# token is generated on first start and persisted to the config dir
# (access_token.txt); later starts reuse the existing token so the access
# link stays stable. Browsers exchange it once for an HttpOnly session
# cookie; every /api/* request must carry that cookie. Static assets stay
# public (they are just the compiled frontend), so the login dialog can
# render before authentication.

AUTH_COOKIE = "trilobite_token"
TOKEN_FILE = "access_token.txt"
auth_token: str | None = None


def ensure_auth_token() -> str:
    """Load the persisted access token, or generate and persist one on first run."""
    global auth_token
    if auth_token is None:
        token_path = get_config_dir() / TOKEN_FILE
        if token_path.is_file():
            existing = token_path.read_text().strip()
            if existing:
                auth_token = existing
        if auth_token is None:
            auth_token = secrets.token_urlsafe(32)
            get_config_dir().mkdir(parents=True, exist_ok=True)
            token_path.write_text(auth_token)
    return auth_token


def _is_authenticated(request: Request) -> bool:
    return secrets.compare_digest(request.cookies.get(AUTH_COOKIE, ""), ensure_auth_token())


@app.middleware("http")
async def auth_middleware(request: Request, call_next):
    path = request.url.path
    if path.startswith("/api/") and not path.startswith("/api/auth/"):
        if not _is_authenticated(request):
            return JSONResponse(status_code=401, content={"detail": "unauthorized"})
    response = await call_next(request)
    if path.startswith("/api/"):
        # API 响应（尤其 SSE 流）一律禁止缓存：没有 Cache-Control 时浏览器
        # 可能对 GET 响应做启发式缓存/重验证。"多开时第二个 tab 卡住"的
        # 根因是 Firefox 对同 URL 在途 GET 的 single-flight 合并，由前端
        # 随机 query 参数绕过（见 api.ts）；no-store 负责其余情况（如中断
        # 的流不被缓存）。
        response.headers["Cache-Control"] = "no-store"
    return response


class AuthRequest(BaseModel):
    key: str


@app.get("/api/auth/status")
async def auth_status(request: Request):
    return {"authenticated": _is_authenticated(request)}


@app.post("/api/auth/login")
async def auth_login(request: Request, req: AuthRequest):
    if not secrets.compare_digest(req.key, ensure_auth_token()):
        raise HTTPException(status_code=401, detail="invalid key")
    response = Response(json.dumps({"status": "ok"}), media_type="application/json")
    response.set_cookie(
        AUTH_COOKIE,
        ensure_auth_token(),
        httponly=True,
        samesite="strict",
        max_age=365 * 24 * 3600,  # 长期有效：token 持久化，cookie 无需频繁重登
    )
    return response

class SessionCreate(BaseModel):
    name: str
    working_dir: str
    project_id: str | None = None

class ProjectCreate(BaseModel):
    name: str
    working_dir: str

class SessionProjectRequest(BaseModel):
    project_id: str | None = None

class RenameRequest(BaseModel):
    name: str

class ImageAttachment(BaseModel):
    mime_type: str
    data_url: str
    original_name: str | None = None


class MessageRequest(BaseModel):
    message: str
    images: list[ImageAttachment] | None = None


class ModeRequest(BaseModel):
    mode: str

class AddDirRequest(BaseModel):
    path: str

class FsWriteRequest(BaseModel):
    path: str
    content: str

class PlanExitRequest(BaseModel):
    approved: bool

class SessionInfo(BaseModel):
    name: str
    working_dir: str
    is_running: bool
    history_length: int


def _decode_data_url(data_url: str) -> bytes:
    m = re.match(r"^data:([^;]+);base64,(.+)$", data_url)
    if not m:
        raise ValueError("invalid data URL")
    return base64.b64decode(m.group(2))


@app.on_event("startup")
async def startup():
    global config, cron_service
    config = init_config()
    # Reload persisted cron schedules and resume the tick loop.
    cron_service = CronService(get_sessions_dir(), config, agents)
    cron_service.load_all()
    cron_service.start()


@app.on_event("shutdown")
async def shutdown():
    if cron_service is not None:
        await cron_service.shutdown()


@app.get("/api/cwd")
async def get_cwd():
    return {"cwd": str(Path.cwd())}


@app.get("/api/version")
async def get_version():
    return {"version": get_pkg_version()}


@app.get("/api/config")
async def get_config():
    return {"enable_vl": load_models(config)[0].enable_vl}


@app.get("/api/models")
async def list_models():
    """The predefined model definitions (frontend shape, no api_key)."""
    return [m.to_frontend_dict() for m in load_models(config)]


def _scheduled_info(info: dict) -> dict:
    """Live schedule state for a scheduled session, read from its owner's
    schedules.json (so a deleted schedule shows up as inactive even after a
    restart, with no extra state to keep in sync). One-shot schedules stay
    in the file after their single fire (``completed``) and cron_delete
    marks entries ``deleted`` instead of removing them, so the endpoint can
    still report the final run state; the frontend renders the dot from
    ``last_state`` (pending / running / error / finished)."""
    parent = info.get("parent_session")
    schedule_id = info.get("schedule_id")
    if not parent or not schedule_id:
        return {"schedule_active": False, "cron": "", "run_count": 0, "last_state": None, "recurring": None, "deleted": False, "next_fire_at": None, "prompt": ""}
    path = get_sessions_dir() / parent / "schedules.json"
    active = False
    cron = ""
    run_count = 0
    last_state = None
    recurring = None
    deleted = False
    next_fire_at = None
    prompt = ""
    if path.is_file():
        try:
            data = json.loads(path.read_text())
            for s in data.get("schedules", []):
                if s.get("id") == schedule_id:
                    active = not s.get("completed", False) and not s.get("deleted", False)
                    cron = s.get("cron", "")
                    run_count = s.get("run_count", 0)
                    last_state = s.get("last_state")
                    recurring = bool(s.get("recurring", True))
                    deleted = bool(s.get("deleted", False))
                    prompt = s.get("prompt", "")
                    if active:
                        nxt = Schedule.from_dict(s).next_fire_at()
                        if nxt is not None:
                            next_fire_at = nxt.strftime("%Y-%m-%d %H:%M")
                    break
        except Exception:
            pass
    return {
        "schedule_active": active,
        "cron": cron,
        "run_count": run_count,
        "last_state": last_state,
        "recurring": recurring,
        "deleted": deleted,
        "next_fire_at": next_fire_at,
        "prompt": prompt,
    }


@app.get("/api/sessions")
async def list_sessions():
    sessions_dir = get_sessions_dir()
    result = []
    if sessions_dir.exists():
        for sd in sorted(sessions_dir.iterdir()):
            if sd.is_dir() and (sd / "session.json").exists():
                try:
                    info = json.loads((sd / "session.json").read_text())
                    info["id"] = sd.name
                    agent = agents.get(sd.name)
                    info["is_running"] = agent.is_running() if agent else False
                    info["history_length"] = len(agent.history) if agent else 0
                    info["plan_mode"] = agent._plan_mode if agent else info.get("plan_mode", False)
                    info["model"] = agent._model_name if agent else info.get("model") or get_default_model_name(config)
                    info["sealed"] = agent.is_sealed() if agent else bool(info.get("subagent_type"))
                    # Scheduled sessions carry their schedule's live state
                    # (cron, run count, last state, whether the schedule still
                    # exists) so the sidebar can render the clock node and a
                    # "stopped" marker after cron_delete.
                    if info.get("kind") == "scheduled":
                        sched_info = _scheduled_info(info)
                        info.update(sched_info)
                    else:
                        # Main sessions: whether the session owns any schedule
                        # that still needs to fire. Feeds the sidebar's blue
                        # dot and top-of-list sorting.
                        info["has_schedule"] = cron_service.has_active(sd.name) if cron_service else False
                    # Last activity: history.json mtime (written at the end of
                    # each run); never-messaged sessions fall back to created_at.
                    hist = sd / "history.json"
                    try:
                        ts = hist.stat().st_mtime if hist.is_file() else None
                    except OSError:
                        ts = None
                    info["updated_at"] = ts or info.get("created_at") or 0
                    result.append(info)
                except Exception:
                    pass
    return result


@app.post("/api/sessions")
async def create_session(req: SessionCreate):
    # The directory name is a stable UUID identifier; the human-readable name
    # lives in session.json and can be renamed freely without moving files.
    session_id = uuid.uuid4().hex
    session_dir = get_sessions_dir() / session_id

    if req.project_id is not None:
        projects = load_projects(get_sessions_dir())
        if not any(p.get("id") == req.project_id for p in projects):
            raise HTTPException(404, "Project not found")

    session_dir.mkdir(parents=True, exist_ok=True)
    info = {"name": req.name, "working_dir": req.working_dir, "plan_mode": False, "additional_dirs": [], "created_at": time.time()}
    info["model"] = get_default_model_name(config)
    if req.project_id:
        info["project_id"] = req.project_id
    (session_dir / "session.json").write_text(json.dumps(info, indent=2))

    agent = Agent(
        name=session_id,
        working_dir=req.working_dir,
        session_dir=session_dir,
        config=config,
        registry=agents,
        cron_service=cron_service,
    )
    info["session_id"] = agent.session_id
    (session_dir / "session.json").write_text(json.dumps(info, indent=2))
    agents[session_id] = agent
    return {"status": "ok", "id": session_id, "name": req.name}


@app.post("/api/sessions/{name}/rename")
async def rename_session(name: str, req: RenameRequest):
    session_dir = get_sessions_dir() / name
    if not session_dir.exists():
        raise HTTPException(404, "Session not found")
    info = json.loads((session_dir / "session.json").read_text())
    info["name"] = req.name
    # A manual rename finalizes the title: the auto-namer must not overwrite a
    # user-chosen name on the first message.
    info["titled"] = True
    (session_dir / "session.json").write_text(json.dumps(info, indent=2))
    return {"status": "ok", "name": req.name}


@app.delete("/api/sessions/{name}")
async def delete_session(name: str):
    # Cascade: also delete child subagent sessions spawned by this one.
    sessions_dir = get_sessions_dir()
    child_names = []
    if sessions_dir.exists():
        for sd in sessions_dir.iterdir():
            if sd.is_dir() and (sd / "session.json").exists():
                try:
                    info = json.loads((sd / "session.json").read_text())
                    if info.get("parent_session") == name:
                        child_names.append(sd.name)
                except Exception:
                    pass
    for n in [name] + child_names:
        if n in agents:
            agents.pop(n)
        sd = sessions_dir / n
        if sd.exists():
            # Deleting a scheduled session removes its schedule from the owner.
            try:
                info = json.loads((sd / "session.json").read_text())
                if info.get("kind") == "scheduled" and cron_service is not None:
                    cron_service.remove_schedule_by_session(n)
            except Exception:
                pass
            import shutil
            shutil.rmtree(sd)
    if cron_service is not None:
        cron_service.remove_session(name)
    return {"status": "ok"}


@app.get("/api/projects")
async def list_projects():
    return load_projects(get_sessions_dir())


@app.post("/api/projects")
async def create_project(req: ProjectCreate):
    project = projects_create(get_sessions_dir(), req.name, req.working_dir)
    return {"status": "ok", "id": project["id"], "name": project["name"]}


@app.delete("/api/projects/{project_id}")
async def delete_project(project_id: str):
    sessions_dir = get_sessions_dir()
    if not projects_delete(sessions_dir, project_id):
        raise HTTPException(404, "Project not found")
    # The project is a grouping only: member sessions are kept and become
    # unassigned (their working_dir is untouched).
    if sessions_dir.exists():
        for sd in sessions_dir.iterdir():
            if sd.is_dir() and (sd / "session.json").exists():
                try:
                    info = json.loads((sd / "session.json").read_text())
                    if info.get("project_id") == project_id:
                        info.pop("project_id", None)
                        (sd / "session.json").write_text(json.dumps(info, indent=2))
                except Exception:
                    pass
    return {"status": "ok"}


@app.put("/api/sessions/{name}/project")
async def set_session_project(name: str, req: SessionProjectRequest):
    """Assign a session to a project (or unassign with null). This only
    changes the sidebar grouping; the session's working_dir is untouched."""
    session_dir = get_sessions_dir() / name
    if not session_dir.exists():
        raise HTTPException(404, "Session not found")
    if req.project_id is not None:
        projects = load_projects(get_sessions_dir())
        if not any(p.get("id") == req.project_id for p in projects):
            raise HTTPException(404, "Project not found")
    info = json.loads((session_dir / "session.json").read_text())
    if req.project_id is None:
        info.pop("project_id", None)
    else:
        info["project_id"] = req.project_id
    (session_dir / "session.json").write_text(json.dumps(info, indent=2))
    return {"status": "ok"}


class ModelSelectRequest(BaseModel):
    model: str


@app.put("/api/sessions/{name}/model")
async def set_session_model(name: str, req: ModelSelectRequest):
    """Switch the session's main model. The choice is persisted to
    session.json and takes effect from the next send (an in-flight run picks
    it up at its next completion call)."""
    try:
        get_model(config, req.model)
    except KeyError:
        raise HTTPException(status_code=400, detail=f"unknown model: {req.model}")
    session_dir = get_sessions_dir() / name
    if not session_dir.exists():
        raise HTTPException(404, "Session not found")
    info = json.loads((session_dir / "session.json").read_text())
    info["model"] = req.model
    (session_dir / "session.json").write_text(json.dumps(info, indent=2))
    agent = agents.get(name)
    if agent is not None and agent.kind == "main":
        agent.apply_model(req.model)
    return {"status": "ok", "model": req.model}


def _get_or_create_agent(name: str) -> Agent:
    """Return the in-memory agent for a session, creating it from disk if needed.

    Shared by the message, stream, cancel and other endpoints so that a session
    is always loaded consistently.
    """
    agent = agents.get(name)
    if agent is not None:
        return agent
    session_dir = get_sessions_dir() / name
    if not session_dir.exists():
        raise HTTPException(404, "Session not found")
    info = json.loads((session_dir / "session.json").read_text())
    subagent_type = info.get("subagent_type")
    if subagent_type:
        # A subagent session restored from disk: rebuild as a sealed, view-only
        # agent (its run is long over; it cannot accept new input).
        agent = Agent(
            name=name,
            working_dir=info["working_dir"],
            session_dir=session_dir,
            config=config,
            session_id=info.get("session_id"),
            registry=agents,
            subagent_type=subagent_type,
            description=info.get("description"),
            depth=info.get("depth", 1),
            sealed=True,
            model_name=info.get("model"),
        )
        agent.set_additional_dirs(info.get("additional_dirs", []))
        agents[name] = agent
        return agent
    if info.get("kind") == "scheduled":
        # A scheduled session restored from disk: rebuild as an idle scheduled
        # agent for viewing. It rejects new messages; the next cron fire
        # reuses (or replaces) it.
        agent = Agent(
            name=name,
            working_dir=info["working_dir"],
            session_dir=session_dir,
            config=config,
            session_id=info.get("session_id"),
            registry=agents,
            scheduled=True,
            scheduled_allow_dirs=info.get("additional_dirs", []),
            max_steps=int(config.get("subagent_max_steps", 100)),
            model_name=info.get("model"),
        )
        agent.set_additional_dirs(info.get("additional_dirs", []))
        agents[name] = agent
        return agent
    agent = Agent(
        name=name,
        working_dir=info["working_dir"],
        session_dir=session_dir,
        config=config,
        session_id=info.get("session_id"),
        registry=agents,
        cron_service=cron_service,
        model_name=info.get("model"),
    )
    agent.set_plan_mode(info.get("plan_mode", False))
    agent.set_additional_dirs(info.get("additional_dirs", []))
    agents[name] = agent
    return agent


@app.post("/api/sessions/{name}/message")
async def send_message(name: str, req: MessageRequest):
    agent = _get_or_create_agent(name)
    if agent.is_scheduled():
        raise HTTPException(status_code=409, detail="scheduled agent does not accept input (view-only; manage it with cron_delete from the main session)")
    if agent.is_sealed():
        raise HTTPException(status_code=409, detail="subagent session has ended, no longer accepts input")
    if req.message.strip() == "/compact":
        if agent.is_running():
            raise HTTPException(status_code=409, detail="agent is running, stop it first")
        await agent.compact_now()
        return {"status": "compacted"}
    if agent.is_running():
        # Steering only carries text; attached images are ignored mid-run.
        await agent.steer(req.message)
        return {"status": "steered"}
    # The agent runs as an independent task; the HTTP response returns
    # immediately. Output is delivered through the /stream subscription
    # endpoint, so closing the browser never cancels an in-progress run.
    if not agent.enable_vl:
        # Image support is disabled for the session's current model: do not
        # save new attachments, but keep any images already stored in the
        # session history.
        req.images = None
    images: list[Image] = []
    for att in req.images or []:
        data = _decode_data_url(att.data_url)
        images.append(save_image(
            agent.session_dir,
            data,
            att.mime_type,
            original_name=att.original_name or "",
        ))
    await agent.start(req.message, images=images or None)
    return {"status": "started"}


class RevertRequest(BaseModel):
    message_id: str
    message: str


class ScheduleDeleteRequest(BaseModel):
    schedule_id: str


@app.post("/api/sessions/{name}/revert")
async def revert_message(name: str, req: RevertRequest):
    agent = _get_or_create_agent(name)
    try:
        status = await agent.revert(req.message_id, req.message)
    except ValueError:
        raise HTTPException(status_code=400, detail="user message not found")
    return {"status": status}


@app.get("/api/sessions/{name}/stream")
async def stream_session(name: str, request: Request):
    agent = _get_or_create_agent(name)
    queue, snapshot = await agent.attach_subscriber()

    async def event_stream():
        try:
            # init carries a consistent snapshot of committed history plus the
            # current run state; the broker then replays the in-progress run's
            # buffered events through this same queue before live events.
            yield f"data: {json.dumps({'type': 'init', **snapshot}, ensure_ascii=False)}\n\n"
            while True:
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=15.0)
                except asyncio.TimeoutError:
                    if await request.is_disconnected():
                        break
                    yield ": keepalive\n\n"
                    continue
                yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
        finally:
            agent.detach_subscriber(queue)

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        # SSE 响应必须禁止一切缓存：没有 Cache-Control 时浏览器可能对 GET
        # 响应做启发式缓存/重验证。多开时"第二个 tab 卡住"的根因（Firefox
        # 对同 URL 在途 GET 的 single-flight 合并）由前端随机 query 绕过。
        headers={"Cache-Control": "no-store"},
    )


@app.post("/api/sessions/{name}/cancel")
async def cancel_session(name: str):
    agent = agents.get(name)
    if agent and agent.is_running():
        agent.cancel()
    return {"status": "ok"}


@app.post("/api/sessions/{name}/schedule/delete")
async def delete_schedule(name: str, req: ScheduleDeleteRequest):
    """Cancel a cron schedule from the UI (same semantics as cron_delete:
    no further fires, the scheduled session stays for review)."""
    if cron_service is None:
        raise HTTPException(status_code=500, detail="cron service not ready")
    result = cron_service.delete_schedule(name, req.schedule_id)
    if result.startswith("Error"):
        raise HTTPException(status_code=404, detail=result)
    return {"status": "ok", "detail": result}


@app.post("/api/sessions/{name}/interrupt")
async def interrupt_session(name: str):
    """Interrupt a running subagent: it stops work and produces a summary."""
    agent = agents.get(name)
    if agent and agent.is_running():
        agent.interrupt()
    return {"status": "ok"}


@app.post("/api/sessions/{name}/plan_exit")
async def plan_exit_decision(name: str, req: PlanExitRequest):
    agent = agents.get(name)
    if agent:
        agent.resolve_plan_exit(req.approved)
    return {"status": "ok"}


@app.post("/api/sessions/{name}/permission")
async def permission_decision(name: str, req: PlanExitRequest):
    agent = agents.get(name)
    if agent:
        agent.resolve_permission(req.approved)
    return {"status": "ok"}


@app.post("/api/sessions/{name}/mode")
async def set_mode(name: str, req: ModeRequest):
    session_dir = get_sessions_dir() / name
    if not session_dir.exists():
        raise HTTPException(404, "Session not found")

    info = json.loads((session_dir / "session.json").read_text())
    if info.get("kind") == "scheduled":
        # Scheduled agents are a fixed role (CronSubagentPermission), not a
        # mode; they must not be switchable from the UI.
        raise HTTPException(400, "scheduled agent has no mode")

    plan_mode = req.mode == "plan"
    info["plan_mode"] = plan_mode
    (session_dir / "session.json").write_text(json.dumps(info, indent=2))

    agent = agents.get(name)
    if agent:
        agent.set_plan_mode(plan_mode)

    return {"status": "ok", "mode": req.mode}


@app.post("/api/sessions/{name}/dirs")
async def add_dir(name: str, req: AddDirRequest):
    session_dir = get_sessions_dir() / name
    if not session_dir.exists():
        raise HTTPException(404, "Session not found")

    info = json.loads((session_dir / "session.json").read_text())
    base = Path(info["working_dir"])
    # Grants are stored canonicalized (normalize_dir) so `/foo/`, `/foo` and
    # `~/foo` collapse into one entry; the string-level dedup below then
    # actually works.
    dirs = [str(normalize_dir(d, base)) for d in info.get("additional_dirs", [])]
    path = str(normalize_dir(req.path, base))
    if path not in dirs:
        dirs.append(path)
    info["additional_dirs"] = dirs
    (session_dir / "session.json").write_text(json.dumps(info, indent=2))

    agent = agents.get(name)
    if agent:
        agent.set_additional_dirs(dirs)

    return {"status": "ok", "additional_dirs": dirs}


@app.delete("/api/sessions/{name}/dirs")
async def remove_dir(name: str, req: AddDirRequest):
    session_dir = get_sessions_dir() / name
    if not session_dir.exists():
        raise HTTPException(404, "Session not found")

    info = json.loads((session_dir / "session.json").read_text())
    base = Path(info["working_dir"])
    path = str(normalize_dir(req.path, base))
    # Compare canonical forms: legacy entries like `/foo/` are removed by a
    # `/foo` request (and rewritten in canonical form).
    dirs = [d for d in info.get("additional_dirs", []) if str(normalize_dir(d, base)) != path]
    info["additional_dirs"] = dirs
    (session_dir / "session.json").write_text(json.dumps(info, indent=2))

    agent = agents.get(name)
    if agent:
        agent.set_additional_dirs(dirs)

    return {"status": "ok", "additional_dirs": dirs}


@app.get("/api/sessions/{name}/info")
async def get_session_info(name: str):
    agent = agents.get(name)
    if agent is None:
        session_dir = get_sessions_dir() / name
        if not session_dir.exists():
            raise HTTPException(404, "Session not found")
        info = json.loads((session_dir / "session.json").read_text())
        return {
            "name": name,
            "working_dir": info["working_dir"],
            "is_running": False,
            "token_count": info.get("token_count", 0),
            "max_context_tokens": int(config.get("max_context_tokens", DEFAULT_MAX_CONTEXT_TOKENS)),
            "plan_mode": info.get("plan_mode", False),
            "additional_dirs": info.get("additional_dirs", []),
            "model": info.get("model") or get_default_model_name(config),
        }
    return {
        "name": name,
        "working_dir": str(agent.working_dir),
        "is_running": agent.is_running(),
        "token_count": agent._token_count,
        "max_context_tokens": agent.max_context_tokens,
        "plan_mode": agent._plan_mode,
        "additional_dirs": [str(d) for d in agent._additional_dirs],
        "model": agent._model_name,
    }


@app.get("/api/sessions/{name}/history")
async def get_history(name: str):
    agent = agents.get(name)
    if agent is not None:
        return agent.history.to_flat_dicts()
    # Agent not loaded: read the file and expand to the flat v1-style list the
    # frontend expects.
    session_dir = get_sessions_dir() / name
    if not session_dir.exists():
        raise HTTPException(404, "Session not found")
    history_path = session_dir / "history.json"
    if history_path.exists():
        from src.trilobite.history import MessageList
        return MessageList(history_path).to_flat_dicts()
    return []


# ── file manager ────────────────────────────────────────────────────────────
# The file manager (issue #49) lets the user browse, diff and edit files in
# the session's workspace directly. It is a user-operated IDE surface, fully
# decoupled from the agent: no history, no permission prompts (paths outside
# the workspace are refused outright), no plan-mode restriction. Every path
# goes through resolve_file_path so the workspace boundary and the sensitive
# file filter are enforced exactly like the agent's file tools.

def _fs_roots(agent: Agent) -> list[Path]:
    return [Path(agent.working_dir)] + [Path(d) for d in agent._additional_dirs]


def _fs_resolve(agent: Agent, path: str) -> Path:
    """Resolve a file-manager path, enforcing the workspace boundary."""
    filepath, error, perm_path = resolve_file_path(path, Path(agent.working_dir), [Path(d) for d in agent._additional_dirs])
    if perm_path or error:
        raise HTTPException(status_code=400, detail=error or "path outside workspace")
    return filepath


def _fs_root_for(agent: Agent, path: Path) -> Path:
    """The workspace root (working_dir or an additional_dir) containing path."""
    for root in _fs_roots(agent):
        try:
            path.relative_to(root)
            return root
        except ValueError:
            continue
    raise HTTPException(status_code=400, detail="path outside workspace")


def _fs_read_text(filepath: Path) -> str:
    """Read a text file for the file manager; binary and oversized files are refused."""
    if not filepath.is_file():
        raise HTTPException(status_code=404, detail="file not found")
    raw = filepath.read_bytes()
    if b"\x00" in raw:
        raise HTTPException(status_code=400, detail="binary file")
    if len(raw) > MAX_FILE_SIZE:
        raise HTTPException(status_code=413, detail="file too large (use the agent's read tool for paging)")
    return raw.decode("utf-8", errors="replace")


@app.get("/api/sessions/{name}/fs/list")
async def fs_list(name: str, path: str, base: str | None = None):
    agent = _get_or_create_agent(name)
    dir_path = _fs_resolve(agent, path)
    if not dir_path.is_dir():
        raise HTTPException(status_code=400, detail="not a directory")
    root = _fs_root_for(agent, dir_path)
    relpath = str(dir_path.relative_to(root)) if dir_path != root else ""
    listing = list_dir(root, relpath, base)
    return {
        "path": str(dir_path),
        "name": dir_path.name or str(dir_path),
        **listing,
    }


@app.get("/api/sessions/{name}/fs/file")
async def fs_read(name: str, path: str):
    agent = _get_or_create_agent(name)
    filepath = _fs_resolve(agent, path)
    return {"path": str(filepath), "content": _fs_read_text(filepath)}


@app.get("/api/sessions/{name}/fs/diff")
async def fs_diff(name: str, path: str, base: str = "master"):
    agent = _get_or_create_agent(name)
    filepath = _fs_resolve(agent, path)
    current = _fs_read_text(filepath)
    root = _fs_root_for(agent, filepath)
    relpath = str(filepath.relative_to(root))
    base_content, error = show_base_content(root, base, relpath)
    if error:
        raise HTTPException(status_code=400, detail=error)
    rows = build_diff_rows(base_content, current)
    if len(rows) > MAX_DIFF_ROWS:
        raise HTTPException(status_code=413, detail="diff too large")
    return {"rows": rows, "base": base, "untracked": base_content is None}


@app.put("/api/sessions/{name}/fs/file")
async def fs_write(name: str, req: FsWriteRequest):
    agent = _get_or_create_agent(name)
    filepath = _fs_resolve(agent, req.path)
    if not filepath.parent.is_dir():
        raise HTTPException(status_code=400, detail="parent directory not found")
    style = "lf"
    if filepath.is_file():
        style = detect_line_ending(_fs_read_text(filepath))
    # Normalize whatever the textarea sent to LF, then restore the original
    # line-ending style (same as the edit tool).
    content = req.content.replace("\r\n", "\n").replace("\r", "\n")
    filepath.write_bytes(materialize(content, style).encode("utf-8"))
    return {"ok": True}


@app.get("/api/sessions/{name}/images/{filename}")
async def get_image(name: str, filename: str):
    session_dir = get_sessions_dir() / name
    if not session_dir.exists():
        raise HTTPException(404, "Session not found")
    # Only serve hash-style filenames under the session's images directory.
    if not re.fullmatch(r"[a-f0-9]{12}\.[a-z0-9]+", filename):
        raise HTTPException(400, "invalid image filename")
    path = session_dir / "images" / filename
    if not path.is_file():
        raise HTTPException(404, "image not found")
    mime = ext_to_mime(path.suffix)
    return Response(path.read_bytes(), media_type=mime)


# Debug page: every icon used by the frontend, standard Unicode vs the
# vendored Material Symbols subset (see frontend/public/debug-icons.html).
# Served outside /api so it works without a token, like the static assets.
@app.get("/debug/icons")
async def debug_icons():
    path = Path(__file__).parent / "static" / "debug-icons.html"
    if not path.is_file():
        raise HTTPException(404, "debug-icons.html not found — rebuild the frontend (npm run build)")
    return HTMLResponse(path.read_text(encoding="utf-8"))


app.mount("/", StaticFiles(directory=Path(__file__).parent / "static", html=True), name="static")


def main():
    import argparse

    parser = argparse.ArgumentParser(
        prog="trilobite",
        description="Trilobite coding agent. 默认启动 web 服务器。",
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("-s", "--server", action="store_true", help="启动 web 服务器（默认）")
    mode.add_argument("-t", "--cli", dest="cli_new", action="store_true", help="命令行交互模式，新建 session")
    mode.add_argument("-c", "--continue", dest="cli_continue", action="store_true", help="命令行交互模式，续接当前目录最新的 session")
    parser.add_argument("working_dir", nargs="?", default=None, help="-t 模式的 working dir，默认 cwd")
    args = parser.parse_args()

    if args.cli_new:
        import asyncio
        import os

        from src.trilobite.cli import run_cli

        asyncio.run(run_cli(args.working_dir or os.getcwd(), resume=False))
        return
    if args.cli_continue:
        import asyncio

        from src.trilobite.cli import run_cli

        asyncio.run(run_cli(None, resume=True))
        return

    # default (no args or -s): web server
    import uvicorn

    cfg = init_config()
    token = ensure_auth_token()
    token_path = get_config_dir() / TOKEN_FILE
    host = str(cfg["host"]).strip()
    port = int(cfg["port"])
    print(f"Trilobite {get_pkg_version()}")
    print(f"Trilobite web UI: http://{host}:{port}/?token={token}")
    print(f"Access key: {token}")
    print(f"Access key saved to {token_path}")
    uvicorn.run(
        app,
        host=host,
        port=port,
        access_log=False,
        log_level=str(cfg.get("log_level", "WARNING")).lower(),
    )


if __name__ == "__main__":
    main()
