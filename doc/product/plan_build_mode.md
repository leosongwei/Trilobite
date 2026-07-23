# Plan / Build 模式

## 概述

Trilobite 有两种工作模式，由前端切换按钮控制：

| 模式 | 说明 | write 工具 |
|------|------|-----------|
| **Build**（默认） | 完整工具执行 | 可用 |
| **Plan** | 只读分析 + 方案设计 | 禁用 |

模式是 Agent 上的一个布尔标记，**只能由用户通过前端切换**。LLM 可以通过 `exit_plan_mode` 工具请求用户切换到 Build 模式，但最终决定权在用户。

## 切换方式

前端有一个 Plan/Build 切换按钮。点击后：

1. 前端调用 `POST /api/sessions/{name}/mode` 设置模式
2. Agent 更新 `_plan_mode` 标记
3. 模式持久化到 `session.json`（重启后恢复）

**通知是动态注入的，不存入 history。** 用户切换模式时不会立即产生任何消息。只有当用户发送消息触发 `run()` 时，才检查模式是否变化。如果变了，在构建 API messages 时动态插入一条通知；如果没变，不打扰模型。

### 注入机制

`run()` 开始时（用户发消息时）检查一次：

```python
mode_notification = None
if self._last_notified_mode is None:          # 首次调用或 session 恢复
    self._last_notified_mode = self._plan_mode # 同步，不注入
elif self._plan_mode != self._last_notified_mode:
    mode_notification = PLAN_MODE_NOTIFICATION or BUILD_MODE_NOTIFICATION
    self._last_notified_mode = self._plan_mode
```

在第一次 API 请求中，将通知插入 messages 列表（system 消息之后），后续工具调用轮次不重复注入：

```python
messages = self.history.get_api_messages()
if mode_notification:
    messages.insert(1, {"role": "user", "content": mode_notification})
    mode_notification = None  # 只注入一次
```

**效果**：
- 用户快速切换 Plan->Build->Plan，净变化为零 -> 不注入任何通知
- 用户切换模式后发消息 -> 注入通知，模型知道模式变了
- 用户切换模式但没发消息 -> 不注入（模型不需要知道）
- 模型在工具调用循环中 -> 不重复注入（只注入一次）
- Session 恢复后首次调用 -> 同步状态，不注入通知

## 通知文案

**进入 Plan 模式**：
```
Your operational mode has changed from build to plan.
You are now in read-only mode.
You are not permitted to make file changes. Focus on exploring, analyzing, and planning.
```

**进入 Build 模式**：
```
Your operational mode has changed from plan to build.
You are no longer in read-only mode.
You are permitted to make file changes, run shell commands, and utilize your arsenal of tools as needed.
```

通知**不持久化在 history 中**，只在 API 请求的 messages 列表中临时存在。history.json 保持干净，前端不会显示通知消息。

## Plan 模式行为

### 工具守卫

Plan/Build 双模式由 **permission 策略**（`permission.py`）实现，而非散落在 agent 里的布尔标记。每种模式是一个 `AgentPermission` 子类，承担两个职责：

1. **过滤工具列表**：决定向 LLM 暴露哪些工具定义。
2. **拦截调用**：在实际执行前拦截被禁用的工具调用。

| 模式 | 暴露的工具 | 拦截 |
|------|-----------|------|
| Build（`BuildModePermission`） | `read` `write` `bash` `TodoList` | 无 |
| Plan（`PlanModePermission`） | `read` `bash` `TodoList` `exit_plan_mode` | `write` |

Plan 模式下 `write` **根本不出现在工具列表里**（LLM 看不到它）。若模型仍幻觉式地调用 `write`，`intercept` 兜底拦截并返回错误：

```
Error: write tool is not available in plan mode. Call exit_plan_mode to request switching to build mode.
```

> 这里刻意区分两类东西：plan/build 是主 agent 的**运行时模式**（在同一个会话里热切换，靠换 permission 实现）；而 explore/general 是未来 subagent 的**声明式角色**（派生时固化，不切换）。它们都是 `AgentPermission` 子类，但生命周期不同，不强行统一成一个"agent 定义 + mode 字段"。

### 模式切换

`Agent._plan_mode` 现在是从当前 permission **派生**的只读 property（`isinstance(self._permission, PlanModePermission)`），不再是独立存储的布尔位。`set_plan_mode(mode)` 通过换 permission 对象完成切换，permission 是单一事实来源。`session.json` 仍持久化 `plan_mode` 布尔值（兼容旧 session），server 启动时调用 `set_plan_mode` 还原。

### exit_plan_mode 工具

`exit_plan_mode` 是一个 virtual tool（定义在 `tool_call.py` 的 `EXIT_PLAN_MODE_DEF`），**只在 Plan 模式下暴露**。Build 模式不暴露它（build 模式下调用它无意义）。其执行（审批流程）在 `Agent` 中处理，不在 `tool_call.execute_tool`，因为它需要 broker / asyncio 机制。

模型可以通过调用 `exit_plan_mode` 工具请求用户切换到 Build 模式。流程：

1. 模型调用 `exit_plan_mode`
2. Agent 发送 `plan_exit_request` SSE 事件
3. Agent 暂停，等待用户决策（`asyncio.Event`）
4. 前端显示审批横幅："Agent requests to switch to Build mode" + Approve/Reject 按钮
5. 用户点击后，前端调用 `POST /api/sessions/{name}/plan_exit`
6. Agent 收到决策，继续执行

| 用户操作 | 结果 |
|----------|------|
| Approve | 换为 `BuildModePermission`，模型收到 "Plan mode exited. All tools are now available." |
| Reject | 保持 Plan 模式，模型收到 "User declined. Continue planning in plan mode." |

### 无循环 reminder

不注入循环 reminder。模式变更通知在用户发消息时注入一次，模型从通知中得知当前模式。如果模型忘记了并尝试 `write`，permission 的 `intercept` 会拦住并提醒它。

## Plan 输出

Plan 模式下，LLM 的方案就是普通的文本回复。没有特殊的 plan 文件、没有审批工具。用户阅读方案后，自行切换到 Build 模式并指示 LLM 执行。

## 实际例子

### 完整流程

```
用户：[切换到 Plan 模式]
用户：帮我设计一个用户登录方案

       -> run() 开始，检测到模式从 Build 变为 Plan
       -> messages 中注入 Plan 通知（不存入 history）

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
       -> messages 中注入 Build 通知（不存入 history）

Agent：调用 write 创建 auth.py
      调用 write 修改 server.py
      调用 bash 运行测试
      "完成了，登录功能已添加。"
```

### history 记录

history.json 中**没有**模式切换通知，只有用户消息和模型回复：

```json
[
  { "role": "system", "content": "..." },
  { "role": "user", "content": "帮我设计一个用户登录方案" },
  { "role": "assistant", "tool_calls": [{"function": {"name": "read", ...}}] },
  { "role": "tool", "content": "..." },
  { "role": "assistant", "content": "建议添加 JWT 认证..." },
  { "role": "user", "content": "按这个方案做吧" },
  { "role": "assistant", "tool_calls": [{"function": {"name": "write", ...}}] },
  { "role": "tool", "content": "..." }
]
```

模式通知只存在于 API 请求的 messages 中，不持久化。

### 快速切换不产生通知

```
用户：[切换到 Plan] [切换到 Build] [切换到 Plan]
用户：帮我看看代码

       -> run() 开始，_plan_mode = True，_last_notified_mode = True
       -> 模式没变，不注入通知
       -> 模型不受打扰
```

## API

### 设置模式

```http
POST /api/sessions/{name}/mode
Content-Type: application/json

{ "mode": "plan" | "build" }
```

### Plan 退出审批

```http
POST /api/sessions/{name}/plan_exit
Content-Type: application/json

{ "approved": true | false }
```

### SSE 事件

模型调用 `exit_plan_mode` 时发送：

```json
{ "type": "plan_exit_request" }
```

### Session Info

`GET /api/sessions/{name}/info` 包含字段：

```json
{ "plan_mode": false }
```
