from pathlib import Path
from typing import Any

from src.trilobite.config import init_config
from src.trilobite.skills import discover_skills
from src.trilobite.tools.tool import Tool


class SkillTool(Tool):
    name = "skill"
    description = """Load the full content of a skill by name.

*When to use:*
- The system prompt lists available skills in an <available_skills> block.
- When the current task matches a skill's purpose, call this tool with the
  skill's name to load its full instructions before proceeding.

*How to use:*
- Pass the exact skill name from the <available_skills> listing.
- After loading, follow the skill's instructions. Relative paths inside the
  skill content are relative to the skill's base directory.

*Rules:*
- Do not call this tool for a skill that is not listed; use read/glob/grep
  on the skill's path instead if you know it."""

    parameters = {
        "type": "object",
        "properties": {
            "name": {
                "type": "string",
                "description": "Name of the skill to load (exact match from the <available_skills> listing).",
            },
        },
        "required": ["name"],
    }

    def execute(
        self,
        working_dir: Path,
        session_dir: Path,
        additional_dirs: list[Path] | None = None,
        name: str = "",
        **kwargs: Any,
    ) -> str:
        """Return the skill's full content, or an error listing available names.

        Discovery re-runs at call time so skills added after the session
        started can still be loaded (the system-prompt listing is fixed at
        session start and only refreshed on a new session).
        """
        if not name:
            return "Error: skill tool requires a 'name' argument."
        skills = discover_skills(Path(working_dir), init_config().get("skill_dirs", []))
        for s in skills:
            if s.name == name:
                if s.builtin:
                    base_line = "This is a built-in skill; it has no files on disk."
                else:
                    base_line = (
                        f"Base directory for this skill: {s.base_dir}\n"
                        "Relative paths in this skill (e.g. scripts/, reference/) are relative to this base directory."
                    )
                return (
                    f'<skill_content name="{s.name}">\n'
                    f"# Skill: {s.name}\n\n"
                    f"{s.content}\n\n"
                    f"{base_line}\n"
                    "</skill_content>"
                )
        available = ", ".join(s.name for s in skills) or "none"
        return f"Error: unknown skill '{name}'. Available skills: {available}"
