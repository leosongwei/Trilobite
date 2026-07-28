import asyncio
import importlib.metadata
import json
import time
import uuid
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from src.trilobite.agent import Agent
from src.trilobite.config import init_config, get_sessions_dir, DEFAULT_MAX_CONTEXT_TOKENS

app = FastAPI(title="Trilobite")

agents: dict[str, Agent] = {}
config: dict = {}

class SessionCreate(BaseModel):
    name: str
    working_dir: str

class RenameRequest(BaseModel):
    name: str

class MessageRequest(BaseModel):
    message: str

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


@app.on_event("startup")
async def startup():
    global config
    config = init_config()


@app.get("/api/cwd")
async def get_cwd():
    return {"cwd": str(Path.cwd())}


@app.get("/api/version")
async def get_version():
    try:
        version = importlib.metadata.version("trilobite-code")
    except importlib.metadata.PackageNotFoundError:
        version = "unknown"
    return {"version": version}


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
        await agent.steer(req.message)
        return {"status": "steered"}
    # The agent runs as an independent task; the HTTP response returns
    # immediately. Output is delivered through the /stream subscription
    # endpoint, so closing the browser never cancels an in-progress run.
    await agent.start(req.message)
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


app.mount("/", StaticFiles(directory=Path(__file__).parent / "static", html=True), name="static")


def main():
    import argparse

    parser = argparse.ArgumentParser(prog="trilobite", description="Trilobite coding agent.")
    parser.add_argument("-c", "--cli", action="store_true", help="启动命令行交互模式（不启动 web 服务器）")
    parser.add_argument("working_dir", nargs="?", default=None, help="CLI 模式的 working dir，默认 cwd")
    args = parser.parse_args()

    if args.cli:
        import asyncio
        import os

        from src.trilobite.cli import run_cli

        asyncio.run(run_cli(args.working_dir or os.getcwd()))
        return

    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=2345)


if __name__ == "__main__":
    main()
