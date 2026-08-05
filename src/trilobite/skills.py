"""Skill discovery and loading (Agent Skills format).

Skills follow the cross-tool "Agent Skills" convention: a directory
``<name>/SKILL.md`` (recommended) or a flat ``<name>.md`` file with YAML
frontmatter. The frontmatter carries the skill's ``name`` (required) and
``description`` (optional, falls back to the first line of the body).

Discovery scans, in priority order (first match wins on name conflicts):

1. builtin skills (``create-skill``; lowest priority, any on-disk skill
   with the same name overrides them)
2. trilobite roots: ``<working_dir>/.trilobite/skills``,
   ``<working_dir>/.agents/skills``, ``<config_dir>/skills``,
   ``~/.agents/skills``, plus extra roots from the ``skill_dirs`` config
   option (``~`` expanded, relative paths resolved against the working
   directory)
3. opencode roots: ``<working_dir>/.opencode/{skill,skills}``,
   ``<xdg_config>/opencode/{skill,skills}``
4. kimi roots: ``<working_dir>/.kimi-code/skills``,
   ``$KIMI_CODE_HOME/skills`` (default ``~/.kimi-code/skills``)
5. claude roots: ``<working_dir>/.claude/skills``, ``~/.claude/skills``

Cross-tool dedupe: a skill found in a higher-priority tool's directory wins
over the same-named skill from a lower-priority tool.

The agent bakes a listing of available skills into its system prompt (see
``format_skill_listing``) and loads the full skill content on demand via the
``skill`` tool (see ``src/trilobite/tools/skill.py``). The listing contains
only name/description/path -- never the body -- so context stays lean until
a skill is actually invoked.
"""

import logging
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from src.trilobite.config import get_config_dir

_log = logging.getLogger("trilobite.skills")

#: frontmatter: opening ``---`` line, YAML block, closing ``---`` line
_FRONTMATTER_RE = re.compile(r"\A---[ \t]*\r?\n(.*?)\r?\n---[ \t]*\r?\n?", re.DOTALL)

#: max length of the description fallback / listing truncation
_DESC_MAX = 240

#: marker path for builtin skills -- never a real file on disk
_BUILTIN_ROOT = Path("<builtin>")


@dataclass
class Skill:
    """A discovered skill: metadata from frontmatter plus the full body."""

    name: str
    description: str
    path: Path
    content: str
    builtin: bool = field(default=False)

    @property
    def base_dir(self) -> Path:
        """Directory the skill lives in; relative paths in the body are
        relative to this directory. Builtin skills have no real directory."""
        return self.path.parent


CREATE_SKILL_CONTENT = """# Create Skill

Use this skill when the user asks you to create, edit, or explain a skill
for this coding agent, or when you notice a repeatable workflow that would
benefit from being packaged as a skill.

## Skill format

A skill is a markdown file with YAML frontmatter (the cross-tool "Agent
Skills" convention):

---
name: skill-name
description: One-line summary of when to use this skill.
---

# Skill Name

Instructions the agent follows when this skill is loaded...

- The frontmatter `name` is required: kebab-case, matching the file or
  directory name. The `description` is optional; when missing, the first
  non-empty line of the body is used (truncated at 240 chars). The body is
  loaded verbatim when the skill tool is called.

## Two file forms

- Directory form (recommended): `<name>/SKILL.md` -- put helper scripts and
  reference files next to the skill; relative paths in the body are
  relative to the skill's directory.
- Flat form: `<name>.md` -- a single-file skill.

## Where to put it

- Project-level: `.trilobite/skills/` or `.agents/skills/` in the working
  directory -- skills that belong to this project, committed with the repo.
- User-level: `~/.config/trilobite/skills/` or `~/.agents/skills/` --
  personal skills used across projects.
- Extra directory listed in `skill_dirs` in config.yaml -- shared team
  skills (relative paths resolve against the working directory, `~` is
  expanded).

Directories of other tools are also scanned, in this priority order:
trilobite > opencode (`.opencode/{skill,skills}`) > kimi
(`.kimi-code/skills`) > claude (`.claude/skills`). A skill with the same
name in a higher-priority directory wins. Prefer the trilobite directories
above when the skill is meant for this agent.

## When to create a skill

- The task has a fixed multi-step procedure with a checklist (code review,
  release, migration, ...).
- Domain knowledge the agent keeps re-deriving.
- Keep skills focused: one procedure per skill; do not bloat a skill with
  unrelated instructions.

## After creating

- The <available_skills> listing in the system prompt is fixed at session
  start; a newly created skill appears in it only in a new session (or
  after the agent restarts). Tell the user this.
- Validate the file you wrote: read it back and confirm the frontmatter
  has a valid `name` and the body is complete.
"""

BUILTIN_SKILLS: list[Skill] = [
    Skill(
        name="create-skill",
        description="Create a new skill (SKILL.md + frontmatter) for this agent, or edit an existing one.",
        path=_BUILTIN_ROOT / "create-skill" / "SKILL.md",
        content=CREATE_SKILL_CONTENT,
        builtin=True,
    ),
]


def skill_roots(working_dir: Path, extra_dirs: list[str] | None = None) -> list[Path]:
    """All directories scanned for skills, in priority order (highest first).

    Sources are grouped by tool so cross-tool skill sets dedupe predictably:
    trilobite > opencode > kimi > claude. Within a tool, project-level roots
    precede user-level roots. ``.agents/skills`` is the cross-tool shared
    directory and counts as a trilobite root (scanned first).
    """
    home = Path.home()
    xdg_config = get_config_dir().parent  # e.g. ~/.config
    kimi_home = Path(os.environ.get("KIMI_CODE_HOME", home / ".kimi-code"))
    roots = [
        # trilobite (highest priority)
        working_dir / ".trilobite" / "skills",
        working_dir / ".agents" / "skills",
        get_config_dir() / "skills",
        home / ".agents" / "skills",
        # opencode (opencode accepts both singular and plural dir names)
        working_dir / ".opencode" / "skills",
        working_dir / ".opencode" / "skill",
        xdg_config / "opencode" / "skills",
        xdg_config / "opencode" / "skill",
        # kimi
        working_dir / ".kimi-code" / "skills",
        kimi_home / "skills",
        # claude
        working_dir / ".claude" / "skills",
        home / ".claude" / "skills",
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
    skipped. Builtin skills are seeded first and any on-disk skill with the
    same name overrides them (lowest priority); among disk roots the first
    occurrence wins -- trilobite > opencode > kimi > claude -- and
    duplicates are logged.
    """
    found: dict[str, Skill] = {s.name: s for s in BUILTIN_SKILLS}
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
                if found[skill.name].builtin:
                    # on-disk skill overrides the builtin of the same name
                    _log.debug("skill %r overrides builtin", skill.name)
                else:
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
        loc = "built-in" if s.builtin else f"at {s.path}"
        lines.append(f'- {s.name}: {_xml_escape(desc)} ({loc})')
    lines.append("</available_skills>")
    return "\n".join(lines)
