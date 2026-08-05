# Plan / Build 模式

## 概述

Trilobite 有两种工作模式，由前端切换按钮控制：

| 模式 | 说明 | edit/write 工具 |
|------|------|-----------|
| **Build**（默认） | 完整工具执行 | 可用 |
| **Plan** | 只读分析 + 方案设计 | 暴露但被拦截 |

模式是 Agent 上的一个布尔标记，**只能由用户通过前端切换**。LLM 可以通过 `exit_plan_mode` 工具请求用户切换到 Build 模式，但最终决定权在用户。

> **工具列表跨模式一致。** 两种模式向 LLM 暴露**完全相同的工具集**（`read` `glob` `grep` `edit` `write` `bash` `TodoList` `exit_plan_mode` `task`），模式差异通过 `<modeswitch>` 提示词消息告知 LLM，并在执行层由 `permission.intercept` 拦截。模式切换时 tools 前缀不变，上下文缓存持续命中。

## 切换方式

前端有一个 Plan/Build 切换按钮。点击后：

1. 前端调用 `POST /api/sessions/{id}/mode` 设置模式
2. Agent 更新 `_plan_mode` 标记
3. 模式持久化到 `session.json`（重启后恢复）

**通知持久化为 history 末尾的 user 消息。** 用户切换模式时不会立即产生任何消息。只有当用户发送消息触发 `run()` 时，才检查模式是否需要通知。如果需要，把 `<modeswitch>` 通知作为一条带 `is_mode_notification` 标记的 user 消息 append 到 history 末尾（在用户的新消息之后）。

> 通知持久化到 history 而非临时插入请求，是为了保持 API 请求前缀单调增长、命中上下文缓存：临时插入到 messages 开头会破坏前缀，且不持久化会导致下一轮前缀再次分叉。通知对前端隐藏（`is_mode_notification` 标记），也不计入 `user_seq`。

### 注入机制

`run()` 开始时（用户发消息时）检查一次：

```python
if self._last_notified_mode is None or self._plan_mode != self._last_notified_mode:
    notif = PLAN_MODE_NOTIFICATION if self._plan_mode else BUILD_MODE_NOTIFICATION
    self._last_notified_mode = self._plan_mode
    self.history.append(UserMessage(notif, is_mode_notification=True))
```

**首次调用（`_last_notified_mode is None`）也注入通知**：新 session 的首轮和 session 恢复后的首轮都携带当前模式，LLM 据此知道处于 plan 还是 build。

通知作为普通 user 消息进入 history，`get_api_messages()` 自然会把它投影进 API 请求（与相邻 user 消息合并）。因为它 append 在末尾，已有前缀不变；持久化后后续每轮前缀一致。

**效果**：
- 用户快速切换 Plan->Build->Plan，净变化为零 -> 不追加任何通知（`_last_notified_mode` 已与 `_plan_mode` 一致）
- 用户切换模式后发消息 -> 追加通知，模型知道模式变了
- 用户切换模式但没发消息 -> 不追加（模型不需要知道）
- Session 恢复后首次调用 -> 追加通知（告知当前模式，因为工具不再反映模式）
- 新 session 首次调用 -> 追加通知

### Compaction 后重新注入

Compaction 会在 history 中插入 `CompactMarker`，`get_api_messages()` 从 marker 之后取消息。marker 之前的 `<modeswitch>` 通知被丢弃，LLM 在压缩后的新上下文中不知道当前模式。因此 `_finalize_compaction()` 在 compact summary 之后追加一条 `<modeswitch>` 通知，并同步 `_last_notified_mode`，避免下一轮重复追加。

## 通知文案

通知采用 `<modeswitch>` 标签格式，明确列出可用/不可用工具。

**进入 Plan 模式**：
```
<modeswitch mode="plan">
You are now in plan mode (read-only analysis).
The following tools are blocked and will be rejected if called: edit, write.
All other tools remain available: read, glob, grep, bash, TodoList, exit_plan_mode, task.
Note: in plan mode the task tool may only spawn explore (read-only) subagents.
Focus on exploring, analyzing, and planning. To make file changes, call exit_plan_mode to request switching to build mode.
</modeswitch>
```

**进入 Build 模式**：
```
<modeswitch mode="build">
You are now in build mode (full access).
All tools are available: read, glob, grep, edit, write, bash, TodoList, task.
(exit_plan_mode is a no-op in build mode and will be rejected if called.)
You may make file changes, run shell commands, and use your full arsenal of tools.
</modeswitch>
```

通知**持久化在 history 中**（作为带 `is_mode_notification` 标记的 user 消息，append 在用户消息之后），但对前端隐藏、不计入 `user_seq`。前端通过 plan/build 切换按钮显示当前模式，无需重复展示通知文本。

system prompt（`SYSTEM_PROMPT`）中有一个 `# Modes` 章节说明 `<modeswitch>` 标签的语义，告知 LLM "以最新的 modeswitch 为准"以及"工具列表跨模式一致"。

## Plan 模式行为

### 工具守卫

Plan/Build 双模式由 **permission 策略**（`permission.py`）实现。两种模式都是 `AgentPermission` 子类，承担两个职责：

1. **工具列表**：两种模式都暴露**完全相同的全量工具集**（`read` `glob` `grep` `edit` `write` `bash` `TodoList` `exit_plan_mode` `task`），保证跨模式切换时 tools 前缀一致、命中缓存。
2. **拦截调用**：`intercept` 在实际执行前拦截被禁用的工具调用，这是模式差异的**唯一执行层保障**。

| 模式 | 暴露的工具 | 拦截 |
|------|-----------|------|
| Build（`BuildModePermission`） | 全量（含 `exit_plan_mode` `task`） | 无 |
| Plan（`PlanModePermission`） | 全量（含 `exit_plan_mode` `task`） | `edit` `write` |

**实现上 `tool_names` 是"允许集"，基类通用 `intercept` 以它为唯一依据做拦截。** 基类 `AgentPermission` 两个可声明字段：

* `tool_names` —— 该策略**允许**的具名工具集（执行层拦截的单一事实来源）。基类通用 `intercept` 检查 `tool_name` 是否在 `tool_names`（外加 `exit_plan_mode`/`task` 两个虚拟工具 flag），被拦时返回 `block_message` 模板（`{tool}` 占位，子类可覆盖文案）。
* `advertised_tool_names` —— **暴露**给 LLM 的工具集，缺省值为 `tool_names`（暴露即允许）。主 agent 两种模式覆盖为 `ALL_TOOL_NAMES`（从 `tool_call.ALL_TOOLS` 派生的全量常量，自动跟随工具注册），跨模式切换 tools 前缀一致；模式差异由 `intercept` 执行。

Plan 模式的声明为三行：`tool_names = (read, glob, grep, bash, TodoList)`、`advertised_tool_names = ALL_TOOL_NAMES`、`block_message = ...`。subagent 角色保持缺省暴露集（暴露集 == 允许集），`intercept` 兜底拦截。

Plan 模式下 `edit`/`write` **仍然出现在工具列表里**（LLM 看得到它们的定义），同时被 `<modeswitch>` 通知告知不可用。若模型仍调用它们，`intercept` 兜底拦截并返回引导消息：

```
Error: edit tool is blocked in plan mode. Call exit_plan_mode to request switching to build mode.
```

> 设计为"提示词引导 + 执行层拦截"：tools 前缀跨模式不变、上下文缓存持续命中，被禁工具由 `intercept` 在执行层拦截（LLM 偶尔误调被禁工具会被拦下，浪费一轮，无副作用）。

> 这里区分两类东西：plan/build 是主 agent 的**运行时模式**（同一会话内热切换，换 permission 实现）；explore/general 是 subagent 的**声明式角色**（派生时固化）。subagent 是独立会话，用定义层过滤工具（角色固定，工具列表不变，缓存正常）。

### 模式切换

`Agent._plan_mode` 是从当前 permission **派生**的只读 property（`isinstance(self._permission, PlanModePermission)`），不再是独立存储的布尔位。`set_plan_mode(mode)` 通过换 permission 对象完成切换，permission 是单一事实来源。`session.json` 仍持久化 `plan_mode` 布尔值（兼容旧 session），server 启动时调用 `set_plan_mode` 还原。

### exit_plan_mode 工具

`exit_plan_mode` 是一个 virtual tool（定义在 `tool_call.py` 的 `EXIT_PLAN_MODE_DEF`），**两种模式都暴露**（为了 tools 前缀一致）。Build 模式下调用它无意义，会在派发时被拒绝并返回 `"exit_plan_mode is a no-op in build mode; you are already in build mode."`。其执行（审批流程）在 `Agent` 中处理，不在 `tool_call.execute_tool`，因为它需要 broker / asyncio 机制。

模型可以通过调用 `exit_plan_mode` 工具请求用户切换到 Build 模式。流程：

1. 模型调用 `exit_plan_mode`
2. Agent 发送 `plan_exit_request` SSE 事件（带 `session` 字段；主 agent 的请求会 fan-out 到其全部运行中子 agent 的 broker，浏览子 session 时也能看到）
3. Agent 暂停，等待用户决策（`asyncio.Event`）
4. 前端把请求加入 pending requests 列表：同一主会话组内无其他 pending 时显示审批横幅 "Agent requests to switch to Build mode" + Approve/Reject 按钮；多个请求并发时横幅聚合显示数量，侧边栏 Pending Requests 列表可单独审批
5. 用户点击后，前端调用 `POST /api/sessions/{id}/plan_exit`
6. Agent 收到决策，继续执行

| 用户操作 | 结果 |
|----------|------|
| Approve | 换为 `BuildModePermission`，模型收到 "Plan mode exited. You are now in build mode and may make file changes." |
| Reject | 保持 Plan 模式，模型收到 "User declined. Continue planning in plan mode." |

> `exit_plan_mode` 审批通过后，下一轮 `run()` 不会追加 `<modeswitch>` 通知：审批通过的 tool_result 已经告知模型切换到 build，且 `_last_notified_mode` 已同步为 False（build）。

### 无循环 reminder

不注入循环 reminder。模式变更通知在用户发消息时注入一次，模型从通知中得知当前模式。如果模型忘记了并尝试 `write`，permission 的 `intercept` 会拦住并提醒它。

## Plan 输出

Plan 模式下，LLM 的方案就是普通的文本回复。没有特殊的 plan 文件、没有审批工具。用户阅读方案后，自行切换到 Build 模式并指示 LLM 执行。

## 实际例子

### 完整流程

```
用户：[切换到 Plan 模式]
用户：帮我设计一个用户登录方案

       -> run() 开始，_last_notified_mode is None（首次），追加 Plan modeswitch
       -> history 末尾追加 <modeswitch mode="plan">（is_mode_notification，前端隐藏）

Agent：调用 read 读取 server.py
      调用 bash 运行 grep -r "auth" src/
      输出方案文本：
      "建议添加 JWT 认证，步骤如下：
       1. 创建 auth.py 实现 token 生成和验证
       2. 在 server.py 添加 /api/login 端点
       ..."

用户：[切换到 Build 模式]
用户：按这个方案做吧

       -> run() 开始，检测到模式从 Plan 变为 Build
       -> history 末尾追加 <modeswitch mode="build">（is_mode_notification，前端隐藏）

Agent：调用 write 创建 auth.py
      调用 write 修改 server.py
      调用 bash 运行测试
      "完成了，登录功能已添加。"
```

### history 记录

模式切换通知作为带 `is_mode_notification` 标记的 user 消息持久化在 history.json 中（紧跟用户消息之后），但对前端隐藏、不计入 `user_seq`：

```json
[
  { "role": "system", "content": "..." },
  { "role": "user", "content": "帮我设计一个用户登录方案" },
  { "role": "user", "content": "<modeswitch mode=\"plan\">...", "is_mode_notification": true },
  { "role": "assistant", "tool_calls": [{"function": {"name": "read", ...}}] },
  { "role": "tool", "content": "..." },
  { "role": "assistant", "content": "建议添加 JWT 认证..." },
  { "role": "user", "content": "按这个方案做吧" },
  { "role": "user", "content": "<modeswitch mode=\"build\">...", "is_mode_notification": true },
  { "role": "assistant", "tool_calls": [{"function": {"name": "edit", ...}}] },
  { "role": "tool", "content": "..." }
]
```

通知持久化是为了保持 API 请求前缀单调增长、命中上下文缓存；前端通过 `is_mode_notification` 标记跳过渲染。

### 快速切换不产生通知

```
用户：[切换到 Plan] [切换到 Build] [切换到 Plan]
用户：帮我看看代码

       -> run() 开始，_plan_mode = True
       -> _last_notified_mode = True（上一轮已通知 plan），模式没变
       -> 不注入通知，模型不受打扰
```

## API

### 设置模式

```http
POST /api/sessions/{id}/mode
Content-Type: application/json

{ "mode": "plan" | "build" }
```

### Plan 退出审批

```http
POST /api/sessions/{id}/plan_exit
Content-Type: application/json

{ "approved": true | false }
```

### SSE 事件

模型调用 `exit_plan_mode` 时发送：

```json
{ "type": "plan_exit_request" }
```

### Session Info

`GET /api/sessions/{id}/info` 包含字段：

```json
{ "plan_mode": false }
```
