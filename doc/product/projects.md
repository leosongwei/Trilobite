# Project（会话分组）

## 概述

Project 是给 session 分组的轻量"文件夹"：sidebar 顶部的 session 列表按 project 分组展示，同时便于用户基于某一工作目录快速创建 session。Project 只记录**名称**和**工作目录**两个要素；成员 session 保留各自的 `working_dir`，仅通过 `project_id` 引用所属 project。改动归属只影响 sidebar 的展示层级，不影响 session 的任何行为（cwd、权限、历史均不变）。

不考虑多层嵌套：session 归属于至多一个 project，project 下只有 session 一层，session 的 subagent 仍挂在对应 session 下展示。

## 数据与持久化

* project 列表持久化在 `get_sessions_dir()/projects.json`（`{"version": 1, "projects": [...]}`），每个 project 含 `id`（uuid）、`name`、`working_dir`、`created_at`。
* session 的归属写入 `session.json` 的 `project_id` 字段；无该字段即不属于任何 project。
* 删除 project 只移除分组：成员 session 保留并变为未分组（`project_id` 被清除，cwd 不动）。

## 后端 API

* `GET /api/projects` — 项目列表。
* `POST /api/projects` — 创建项目 `{name, working_dir}`。
* `DELETE /api/projects/{id}` — 删除项目，成员 session 自动解除归属。
* `PUT /api/sessions/{name}/project` — 设置/解除 session 归属 `{project_id: string | null}`（project 不存在返回 404）。
* `POST /api/sessions` 接受可选 `project_id`，创建时直接归属项目。

## 前端（sidebar）

* **创建**：sidebar 顶部 "+ New Session" 同一行有 "+ New Project" 按钮，复用同一组名称/工作目录输入框。
* **展示**：项目行显示在 session 列表顶部（创建顺序），前面有展开箭头（`ms-expand`，折叠时旋转 90°）和文件夹图标（`ms-folder`）；点击行展开/收起（收起状态仅存内存，刷新即恢复展开）。
* **项目行操作**：行尾 hover 出现 `+`（以项目的名称和工作目录创建 session 并归属于该项目，创建后自动选中）和 `×`（删除项目，确认后成员 session 保留为未分组）。
* **缩进层级**：项目下 session 再缩进一层（`project-session`），其 subagent 继续缩进（`project-child`），与普通 session 的 subagent 缩进相对关系一致。
* **归属选择**：session 信息面板（Session/cwd/Allowed directories 那组）新增 `project:` 下拉行，可把当前主 session 归属到任意项目或 "(none)"；subagent / scheduled session 不显示该下拉（它们始终挂在父 session 下）。
* 引用已删除项目的 session（残留 `project_id`）按未分组展示，不丢失。
