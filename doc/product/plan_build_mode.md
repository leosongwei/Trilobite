# Plan / Build 模式

## 概述

Trilobite 有两种工作模式，由前端切换按钮控制：

| 模式 | 说明 | write 工具 |
|------|------|-----------|
| **Build**（默认） | 完整工具执行 | 可用 |
| **Plan** | 只读分析 + 方案设计 | 禁用 |

模式是 Agent 上的一个布尔标记，**只能由用户通过前端切换**。LLM 没有任何工具可以切换模式。

## 切换方式

前端有一个 Plan/Build 切换按钮。点击后：

1. 前端调用 `POST /api/sessions/{name}/mode` 设置模式
2. Agent 更新 `_plan_mode` 标记
3. 发送 `mode_changed` SSE 事件通知前端更新 UI

**通知是延迟追加的**：不在切换时立即写入 history，而是在下一次 API 请求构建 messages 前检查。这样反复切换（如 Plan→Build→Plan）不会产生噪音消息。

### 延迟检查机制

Agent 维护 `_last_notified_mode`（上次通知模型的状态）和 `_plan_mode`（当前实际状态）。在 `run()` 循环内、每次调用 `get_api_messages()` 之前检查：

```
if _last_notified_mode is None:     # 首次调用或 session 恢复
    _last_notified_mode = _plan_mode  # 同步，不追加通知
elif _plan_mode != _last_notified_mode:
    追加模式切换通知到 history
    _last_notified_mode = _plan_mode
```

**效果**：
- 用户快速切换 Plan→Build→Plan，净变化为零 → 不追加任何通知
- 用户在模型运行中切换，下一轮 API 调用时才追加通知 → 模型不会被中途打扰
- Session 恢复后首次调用，`_last_notified_mode` 为 None → 同步为当前模式，不追加通知（模型从 history 中已有的通知得知状态）

## 模式切换通知

当延迟检查发现模式真正变化时，往 history 追加一条 `role: "user"` 消息：

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

这条消息持久化在 history 中，后续所有轮次都能看到。由于延迟检查机制，只有净变化才会产生通知。

## Plan 模式行为

### 工具守卫

Plan 模式下，`write` 工具被执行前拦截，返回错误：

```
Error: write tool is not available in plan mode. Switch to build mode to make changes.
```

其他工具（`read`、`bash`、`TodoList`）正常执行。

### 无额外 reminder

不注入循环 reminder。模式切换通知已持久化在 history 中，LLM 在后续轮次都能看到。如果 LLM 忘记了并尝试 `write`，工具拒绝消息会提醒它。

## Plan 输出

Plan 模式下，LLM 的方案就是普通的文本回复。没有特殊的 plan 文件、没有审批工具。用户阅读方案后，自行切换到 Build 模式并指示 LLM 执行。

## 实际例子

### 完整流程

```
用户：[切换到 Plan 模式]
用户：帮我设计一个用户登录方案

Agent：调用 read 读取 server.py
      调用 bash 运行 grep -r "auth" src/
      输出方案文本：
      "建议添加 JWT 认证，步骤如下：
       1. 创建 auth.py 实现 token 生成和验证
       2. 在 server.py 添加 /api/login 端点
       ..."

用户：[切换到 Build 模式]
用户：按这个方案做吧

Agent：调用 write 创建 auth.py
      调用 write 修改 server.py
      调用 bash 运行测试
      "完成了，登录功能已添加。"
```

### history 记录

```json
[
  { "role": "system", "content": "..." },
  { "role": "user", "content": "Your operational mode has changed from build to plan.\nYou are now in read-only mode.\nYou are not permitted to make file changes. Focus on exploring, analyzing, and planning." },
  { "role": "user", "content": "帮我设计一个用户登录方案" },
  { "role": "assistant", "tool_calls": [{"function": {"name": "read", ...}}] },
  { "role": "tool", "content": "..." },
  { "role": "assistant", "content": "建议添加 JWT 认证..." },
  { "role": "user", "content": "Your operational mode has changed from plan to build.\nYou are no longer in read-only mode.\nYou are permitted to make file changes, run shell commands, and utilize your arsenal of tools as needed." },
  { "role": "user", "content": "按这个方案做吧" },
  { "role": "assistant", "tool_calls": [{"function": {"name": "write", ...}}] },
  { "role": "tool", "content": "..." }
]
```

注意：模式切换通知和用户消息是分开的 user 消息，`get_api_messages()` 会自动合并连续 user 消息。

## API

### 设置模式

```http
POST /api/sessions/{name}/mode
Content-Type: application/json

{ "mode": "plan" | "build" }
```

### SSE 事件

模式切换时发送：

```json
{ "type": "mode_changed", "mode": "plan" | "build" }
```

### Session Info

`GET /api/sessions/{name}/info` 新增字段：

```json
{ "plan_mode": false }
```
