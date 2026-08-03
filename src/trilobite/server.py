import asyncio
import base64
import json
import re
import secrets
import time
import uuid
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from src.trilobite.agent import Agent
from src.trilobite.config import init_config, get_config_dir, get_sessions_dir, DEFAULT_MAX_CONTEXT_TOKENS
from src.trilobite.image_storage import ext_to_mime, save_image
from src.trilobite.messages import Image
from src.trilobite.version import get_version as get_pkg_version

app = FastAPI(title="Trilobite")

agents: dict[str, Agent] = {}
config: dict = {}

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
    return await call_next(request)


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
    response.set_cookie(AUTH_COOKIE, ensure_auth_token(), httponly=True, samesite="strict")
    return response

class SessionCreate(BaseModel):
    name: str
    working_dir: str

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
    global config
    config = init_config()


@app.get("/api/cwd")
async def get_cwd():
    return {"cwd": str(Path.cwd())}


@app.get("/api/version")
async def get_version():
    return {"version": get_pkg_version()}


@app.get("/api/config")
async def get_config():
    return {"enable_vl": bool(config.get("enable_vl", False))}


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
                    info["sealed"] = agent.is_sealed() if agent else bool(info.get("subagent_type"))
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

    session_dir.mkdir(parents=True, exist_ok=True)
    info = {"name": req.name, "working_dir": req.working_dir, "plan_mode": False, "additional_dirs": [], "created_at": time.time()}
    (session_dir / "session.json").write_text(json.dumps(info, indent=2))

    agent = Agent(
        name=session_id,
        working_dir=req.working_dir,
        session_dir=session_dir,
        config=config,
        registry=agents,
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
            import shutil
            shutil.rmtree(sd)
    return {"status": "ok"}


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
    )
    agent.set_plan_mode(info.get("plan_mode", False))
    agent.set_additional_dirs(info.get("additional_dirs", []))
    agents[name] = agent
    return agent


@app.post("/api/sessions/{name}/message")
async def send_message(name: str, req: MessageRequest):
    agent = _get_or_create_agent(name)
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
    if not config.get("enable_vl", False):
        # Image support is disabled: do not save new attachments, but keep any
        # images already stored in the session history.
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
    user_seq: int
    message: str


@app.post("/api/sessions/{name}/revert")
async def revert_message(name: str, req: RevertRequest):
    agent = _get_or_create_agent(name)
    try:
        status = await agent.revert(req.user_seq, req.message)
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

    return StreamingResponse(event_stream(), media_type="text/event-stream")


@app.post("/api/sessions/{name}/cancel")
async def cancel_session(name: str):
    agent = agents.get(name)
    if agent and agent.is_running():
        agent.cancel()
    return {"status": "ok"}


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

    plan_mode = req.mode == "plan"
    info = json.loads((session_dir / "session.json").read_text())
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
    dirs = info.get("additional_dirs", [])
    if req.path not in dirs:
        dirs.append(req.path)
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
    dirs = info.get("additional_dirs", [])
    dirs = [d for d in dirs if d != req.path]
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
        }
    return {
        "name": name,
        "working_dir": str(agent.working_dir),
        "is_running": agent.is_running(),
        "token_count": agent._token_count,
        "max_context_tokens": agent.max_context_tokens,
        "plan_mode": agent._plan_mode,
        "additional_dirs": [str(d) for d in agent._additional_dirs],
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
        from src.trilobite.history import History
        return History(history_path).to_flat_dicts()
    return []


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
    print(f"Trilobite {get_pkg_version()}")
    print(f"Trilobite web UI: http://127.0.0.1:2345/?token={token}")
    print(f"Access key: {token}")
    print(f"Access key saved to {token_path}")
    uvicorn.run(
        app,
        host="0.0.0.0",
        port=2345,
        access_log=False,
        log_level=str(cfg.get("log_level", "WARNING")).lower(),
    )


if __name__ == "__main__":
    main()
