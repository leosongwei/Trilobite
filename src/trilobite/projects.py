"""Project persistence.

Projects are lightweight session-grouping folders: each project records a
name and a working directory (used as the default when creating sessions
from it), while member sessions keep their own working_dir and merely
reference the project id in session.json. Deleting a project only removes
the grouping - member sessions stay as unassigned sessions.

Projects live in a single ``projects.json`` next to the session
directories (``get_sessions_dir()``), so copying the sessions directory
also carries the grouping.
"""

import json
import time
import uuid
from pathlib import Path

PROJECTS_FILE = "projects.json"


def load_projects(sessions_dir: Path) -> list[dict]:
    path = sessions_dir / PROJECTS_FILE
    if not path.is_file():
        return []
    try:
        data = json.loads(path.read_text())
        return data.get("projects", [])
    except Exception:
        return []


def save_projects(sessions_dir: Path, projects: list[dict]) -> None:
    (sessions_dir / PROJECTS_FILE).write_text(
        json.dumps({"version": 1, "projects": projects}, indent=2)
    )


def create_project(sessions_dir: Path, name: str, working_dir: str) -> dict:
    projects = load_projects(sessions_dir)
    project = {
        "id": uuid.uuid4().hex,
        "name": name,
        "working_dir": working_dir,
        "created_at": time.time(),
    }
    projects.append(project)
    save_projects(sessions_dir, projects)
    return project


def delete_project(sessions_dir: Path, project_id: str) -> bool:
    """Remove a project. Returns True if it existed, False otherwise."""
    projects = load_projects(sessions_dir)
    remaining = [p for p in projects if p.get("id") != project_id]
    if len(remaining) == len(projects):
        return False
    save_projects(sessions_dir, remaining)
    return True
