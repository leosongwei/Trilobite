import asyncio
import json
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

class MessageRequest(BaseModel):
    message: str

class ModeRequest(BaseModel):
    mode: str

class SessionInfo(BaseModel):
    name: str
    working_dir: str
    is_running: bool
    history_length: int


@app.on_event("startup")
async def startup():
    global config
    config = init_config()


@app.get("/api/sessions")
async def list_sessions():
    sessions_dir = get_sessions_dir()
    result = []
    if sessions_dir.exists():
        for sd in sorted(sessions_dir.iterdir()):
            if sd.is_dir() and (sd / "session.json").exists():
                try:
                    info = json.loads((sd / "session.json").read_text())
                    agent = agents.get(sd.name)
                    info["is_running"] = agent.is_running() if agent else False
                    info["history_length"] = len(agent.history) if agent else 0
                    info["plan_mode"] = agent._plan_mode if agent else info.get("plan_mode", False)
                    result.append(info)
                except Exception:
                    pass
    return result


@app.post("/api/sessions")
async def create_session(req: SessionCreate):
    session_dir = get_sessions_dir() / req.name
    if session_dir.exists():
        raise HTTPException(400, "Session already exists")

    session_dir.mkdir(parents=True, exist_ok=True)
    info = {"name": req.name, "working_dir": req.working_dir, "plan_mode": False}
    (session_dir / "session.json").write_text(json.dumps(info, indent=2))

    agent = Agent(
        name=req.name,
        working_dir=req.working_dir,
        session_dir=session_dir,
        config=config,
    )
    agents[req.name] = agent
    return {"status": "ok", "name": req.name}


@app.delete("/api/sessions/{name}")
async def delete_session(name: str):
    if name in agents:
        agents.pop(name)
    session_dir = get_sessions_dir() / name
    if session_dir.exists():
        import shutil
        shutil.rmtree(session_dir)
    return {"status": "ok"}


@app.post("/api/sessions/{name}/message")
async def send_message(name: str, req: MessageRequest):
    agent = agents.get(name)
    if agent is None:
        session_dir = get_sessions_dir() / name
        if not session_dir.exists():
            raise HTTPException(404, "Session not found")
        info = json.loads((session_dir / "session.json").read_text())
        agent = Agent(
            name=name,
            working_dir=info["working_dir"],
            session_dir=session_dir,
            config=config,
        )
        agent.set_plan_mode(info.get("plan_mode", False))
        agents[name] = agent

    if agent.is_running():
        agent.steer(req.message)
        return {"status": "steered"}

    agent.add_user_message(req.message)
    stream_queue: asyncio.Queue[dict] = asyncio.Queue()

    async def event_stream():
        task = asyncio.create_task(agent.run(stream_queue))
        try:
            while True:
                event = await stream_queue.get()
                yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
                if event.get("type") in ("done", "error", "cancelled"):
                    break
        finally:
            if not task.done():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass

    return StreamingResponse(event_stream(), media_type="text/event-stream")


@app.post("/api/sessions/{name}/cancel")
async def cancel_session(name: str):
    agent = agents.get(name)
    if agent and agent.is_running():
        agent.cancel()
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
            "token_count": 0,
            "max_context_tokens": int(config.get("max_context_tokens", DEFAULT_MAX_CONTEXT_TOKENS)),
            "plan_mode": info.get("plan_mode", False),
        }
    return {
        "name": name,
        "working_dir": str(agent.working_dir),
        "is_running": agent.is_running(),
        "token_count": agent._token_count,
        "max_context_tokens": agent.max_context_tokens,
        "plan_mode": agent._plan_mode,
    }


@app.get("/api/sessions/{name}/history")
async def get_history(name: str):
    agent = agents.get(name)
    if agent is None:
        session_dir = get_sessions_dir() / name
        if not session_dir.exists():
            raise HTTPException(404, "Session not found")
        history_path = session_dir / "history.json"
        if history_path.exists():
            return json.loads(history_path.read_text())
        return []
    return agent.history.raw


app.mount("/", StaticFiles(directory=Path(__file__).parent / "static", html=True), name="static")


def main():
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)


if __name__ == "__main__":
    main()
