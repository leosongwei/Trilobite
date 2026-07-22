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

Plan 模式下，`write` 工具被执行前拦截，返回错误：

```
Error: write tool is not available in plan mode. Switch to build mode to make changes.
```

其他工具（`read`、`bash`、`TodoList`）正常执行。工具守卫在每轮工具调用中都生效，与通知注入无关。

### 无循环 reminder

不注入循环 reminder。模式变更通知在用户发消息时注入一次，模型从通知中得知当前模式。如果模型忘记了并尝试 `write`，工具拒绝消息会提醒它。

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

### Session Info

`GET /api/sessions/{name}/info` 包含字段：

```json
{ "plan_mode": false }
```
