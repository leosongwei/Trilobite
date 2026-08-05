"""Skill discovery and loading (Agent Skills format).

Skills follow the cross-tool "Agent Skills" convention: a directory
``<name>/SKILL.md`` (recommended) or a flat ``<name>.md`` file with YAML
frontmatter. The frontmatter carries the skill's ``name`` (required) and
``description`` (optional, falls back to the first line of the body).

Discovery scans, in order (first match wins on name conflicts):

1. project roots: ``<working_dir>/.trilobite/skills``, ``<working_dir>/.agents/skills``
2. user roots: ``<config_dir>/skills`` (``~/.config/trilobite/skills``), ``~/.agents/skills``
3. extra roots from the ``skill_dirs`` config option (``~`` expanded,
   relative paths resolved against the working directory)

The agent bakes a listing of available skills into its system prompt (see
``format_skill_listing``) and loads the full skill content on demand via the
``skill`` tool (see ``src/trilobite/tools/skill.py``). The listing contains
only name/description/path -- never the body -- so context stays lean until
a skill is actually invoked.
"""

import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from src.trilobite.config import get_config_dir

_log = logging.getLogger("trilobite.skills")

#: frontmatter: opening ``---`` line, YAML block, closing ``---`` line
_FRONTMATTER_RE = re.compile(r"\A---[ \t]*\r?\n(.*?)\r?\n---[ \t]*\r?\n?", re.DOTALL)

#: max length of the description fallback / listing truncation
_DESC_MAX = 240


@dataclass
class Skill:
    """A discovered skill: metadata from frontmatter plus the full body."""

    name: str
    description: str
    path: Path
    content: str

    @property
    def base_dir(self) -> Path:
        """Directory the skill lives in; relative paths in the body are
        relative to this directory."""
        return self.path.parent


def skill_roots(working_dir: Path, extra_dirs: list[str] | None = None) -> list[Path]:
    """All directories scanned for skills, project roots first."""
    roots = [
        working_dir / ".trilobite" / "skills",
        working_dir / ".agents" / "skills",
        get_config_dir() / "skills",
        Path.home() / ".agents" / "skills",
    ]
    for d in extra_dirs or []:
        p = Path(d).expanduser()
        if not p.is_absolute():
            p = working_dir / p
        roots.append(p)
    return roots


def parse_skill(path: Path, fallback_name: str | None = None) -> Skill | None:
    """Parse a single skill file (``<name>/SKILL.md`` or ``<name>.md``).

    Returns ``None`` when the file is unreadable or has no usable name.
    The name comes from the frontmatter when present; otherwise it falls
    back to ``fallback_name`` (the directory name for ``SKILL.md`` files,
    the file stem for flat ``<name>.md`` files). The body is the markdown
    after the frontmatter; when the frontmatter is missing entirely the
    whole file is treated as body.
    """
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        _log.warning("skill file unreadable: %s", path)
        return None
    m = _FRONTMATTER_RE.match(text)
    if m:
        try:
            data = yaml.safe_load(m.group(1))
        except yaml.YAMLError:
            data = None
        if not isinstance(data, dict):
            data = {}
        body = text[m.end():].strip()
        name = data.get("name")
        if not isinstance(name, str) or not name.strip():
            name = fallback_name
        description = data.get("description")
    else:
        body = text.strip()
        name = fallback_name
        description = None
    if not name or not name.strip():
        _log.warning("skill %s has no valid 'name', skipped", path)
        return None
    if not isinstance(description, str) or not description.strip():
        first = next((ln.strip() for ln in body.splitlines() if ln.strip()), "")
        description = first.lstrip("# ").strip()
    return Skill(name=name.strip(), description=description[: _DESC_MAX], path=path, content=body)


def discover_skills(working_dir: Path, extra_dirs: list[str] | None = None) -> list[Skill]:
    """Scan all skill roots and return the discovered skills.

    Both file forms are recognized under each root: ``<name>/SKILL.md``
    (directory form) and ``<name>.md`` (flat form). Hidden entries are
    skipped. On name conflicts the first occurrence wins (project roots are
    scanned before user roots); duplicates are logged.
    """
    found: dict[str, Skill] = {}
    for root in skill_roots(working_dir, extra_dirs):
        if not root.is_dir():
            continue
        entries = sorted(root.iterdir(), key=lambda p: p.name.lower())
        for entry in entries:
            if entry.name.startswith("."):
                continue
            skill: Skill | None = None
            if entry.is_dir():
                md = entry / "SKILL.md"
                if md.is_file():
                    skill = parse_skill(md, fallback_name=entry.name)
                    if skill is not None and skill.name != entry.name:
                        _log.debug(
                            "skill %s: frontmatter name %r differs from directory %r",
                            md, skill.name, entry.name,
                        )
            elif entry.suffix == ".md" and entry.name != "SKILL.md":
                skill = parse_skill(entry, fallback_name=entry.stem)
            if skill is None:
                continue
            if skill.name in found:
                _log.warning(
                    "duplicate skill %r: %s wins over %s",
                    skill.name, found[skill.name].path, skill.path,
                )
                continue
            found[skill.name] = skill
    return list(found.values())


def _xml_escape(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


def format_skill_listing(skills: list[Skill]) -> str | None:
    """Render the ``<available_skills>`` block for the system prompt.

    Only name/description/path are listed -- loading the full body is the
    job of the ``skill`` tool. Returns ``None`` when there is nothing to
    list so the agent can skip the block entirely.
    """
    if not skills:
        return None
    lines = [
        "<available_skills>",
        "The following skills are available. When a task matches a skill's purpose, call the skill tool with the skill's name to load its full instructions.",
    ]
    for s in sorted(skills, key=lambda s: s.name):
        desc = " ".join(s.description.split())[: _DESC_MAX]
        lines.append(f'- {s.name}: {_xml_escape(desc)} (at {s.path})')
    lines.append("</available_skills>")
    return "\n".join(lines)
