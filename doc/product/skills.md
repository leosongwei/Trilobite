# Skills

## 概述

Skills 采用跨工具通用的 **Agent Skills 格式**（`SKILL.md` + YAML frontmatter，与 Claude Code / opencode / Kimi CLI 生态兼容）：一个 skill 是一段结构化指令，模型在任务匹配时按需加载并遵循。skill 的完整正文不常驻上下文——system prompt 只注入清单（name/description/path），正文在模型调用 `skill` 工具时才进入对话，保持上下文精简。

## Skill 文件格式

每个 skill 是一个 markdown 文件，frontmatter 支持两个字段：

```markdown
---
name: code-review
description: Review code changes for bugs.
---

# Code Review

检查清单：
- off-by-one 错误
- 边界条件
```

| 字段 | 必需 | 说明 |
|------|------|------|
| `name` | 是 | skill 名，`skill` 工具按它精确匹配；缺失时回退为目录名（目录形式）或文件名（平铺形式） |
| `description` | 否 | 显示在清单里；缺失时取正文首个非空行（去掉前导 `#`），截断 240 字符 |

两种文件形态，放在任一 skill 根目录下：

* **目录形式（推荐）**：`<name>/SKILL.md`——skill 的脚本、参考文件可放在同目录，正文中的相对路径以该目录为基准
* **平铺形式**：`<name>.md`——单个文件即一个 skill

frontmatter 缺失时整个文件当作正文；无可用 name 的 skill 被跳过。

## 发现路径

按优先级从高到低扫描以下根目录（同名冲突时先扫到的胜出，重复打 warning）：

| 优先级 | 目录 | 项目级 | 用户级 |
|--------|------|--------|--------|
| 1 | **`.agents`**（跨工具共享，最高） | `<working_dir>/.agents/skills` | `~/.agents/skills` |
| 2 | **trilobite** | `<working_dir>/.trilobite/skills` | `<config_dir>/skills`（默认 `~/.config/trilobite/skills`），外加 `config.yaml` 的 `skill_dirs` 列表（相对路径基于工作目录，支持 `~` 展开） |
| 3 | **opencode** | `<working_dir>/.opencode/{skill,skills}` | `<xdg_config>/opencode/{skill,skills}`（opencode 兼容单复数目录名） |
| 4 | **kimi** | `<working_dir>/.kimi-code/skills` | `$KIMI_CODE_HOME/skills`（默认 `~/.kimi-code/skills`） |
| 5 | **claude** | `<working_dir>/.claude/skills` | `~/.claude/skills` |

跨工具去重：同一 skill 名出现在多个目录时，高优先级目录的版本胜出（`.agents` > trilobite > opencode > kimi > claude）。内置 `create-skill` 在扫描前注册、优先级最低，磁盘同名 skill 覆盖它。隐藏条目（`.` 开头）跳过。

## 内置 skills

内置 `create-skill`（教模型如何创建/修改 skill：格式、frontmatter 字段、两种文件形态、放置位置、命名与验证），保证任何会话至少有一个可用 skill。内置 skill 优先级最低：磁盘上出现同名 skill 时覆盖内置版本（用户可自定义 create-skill 的行为）。内置 skill 没有磁盘文件，加载时 `skill` 工具会注明 "built-in"。

## 暴露方式

### system prompt 清单

Agent 初始化时发现 skills，把清单追加到 system prompt（紧跟在环境块、系统提示词之后，AGENTS.md 之前），格式：

```
<available_skills>
The following skills are available. When a task matches a skill's purpose, call the skill tool with the skill's name to load its full instructions.
- create-skill: Create a new skill (SKILL.md + frontmatter) for this agent, or edit an existing one. (built-in)
- code-review: Review code changes for bugs. (at /path/to/skills/code-review/SKILL.md)
- deploy: Deploy the service. (at /path/to/team-skills/deploy/SKILL.md)
</available_skills>
```

清单至少包含内置 `create-skill`；内置条目标注 `built-in`。清单只含 name/description/path，不含正文。

### `skill` 工具

模型按清单中的名字调用 `skill` 工具加载全文，工具返回：

```
<skill_content name="code-review">
# Skill: code-review

<skill 正文>

Base directory for this skill: /path/to/skills/code-review
Relative paths in this skill (e.g. scripts/, reference/) are relative to this base directory.
</skill_content>
```

`skill` 是只读工具，plan 模式、explore/general subagent 均可用。执行时重新发现（而非使用启动时的缓存），因此会话中途新增的 skill 也能被加载；工具执行不涉及工作区外文件访问（skill 目录本身在启动时已被读取）。

## 生命周期

清单在会话启动时构建并写入 system 消息，之后不再变化（与 env 块、AGENTS.md 同理）；compaction 重建 system 消息时按同一份 `system_prompt` 重新生成。新增/修改 skill 需要新开 session（或重启）才会反映到清单中；已加载的正文不受影响。

## 设计取舍

* 清单注入 system prompt 而非工具描述：工具 description 有长度上限，且清单与 env 块/AGENTS.md 一样随会话固化，保持 API 前缀稳定、上下文缓存持续命中
* 不自动加载正文：模型只在任务匹配时调用 `skill` 工具，避免无关 skill 占满上下文
* 内置 skill 用代码常量而非磁盘文件：与提示词同语义（行为随代码版本化），用户仍可用同名磁盘 skill 覆盖
* 不支持 sub-skill（`parent.child` 嵌套）与 URL 形式 skill；`skill_dirs` 覆盖本地目录场景
