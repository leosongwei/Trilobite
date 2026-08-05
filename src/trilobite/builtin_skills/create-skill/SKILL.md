---
name: create-skill
description: Create a new skill (SKILL.md + frontmatter) for this agent, or edit an existing one.
---

# Create Skill

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

- Project-level (default): `.agents/skills/` in the working directory --
  highest priority and shared with other agents (Claude Code, Codex, ...),
  so the skill works for anyone developing this repo. Fall back to
  `.trilobite/skills/` only when the skill is Trilobite-specific and should
  stay out of other agents' sight.
- User-level: `~/.agents/skills/` (highest priority) or
  `~/.config/trilobite/skills/` -- personal skills used across projects.
- Extra directory listed in `skill_dirs` in config.yaml -- shared team
  skills (relative paths resolve against the working directory, `~` is
  expanded).

Directories of other tools are also scanned, in this priority order:
`.agents` > trilobite > opencode (`.opencode/{skill,skills}`) > kimi
(`.kimi-code/skills`) > claude (`.claude/skills`). A skill with the same
name in a higher-priority directory wins.

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
