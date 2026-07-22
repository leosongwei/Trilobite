# 流式订阅（Streaming）

## 概述

前后端之间的实时输出采用 **订阅式 SSE**，而非请求式。核心原则：

* **Agent 运行独立于 HTTP 请求**：`POST /message` 只负责启动/转向并立即返回 JSON；agent 作为独立 `asyncio.Task` 运行。关闭浏览器只断开 SSE 订阅，**不会取消正在进行的 agent 运行**。
* **事件总线广播**：每个 session 拥有一个 `StreamBroker`，agent 把事件 publish 到 broker，所有订阅该 session 的客户端都会收到。因此浏览器多开、关闭后重开都能看到同一份输出。
* **重连一致性**：客户端连接时收到 `init` 快照（已提交历史 + 运行状态）+ 当前 run 的事件回放，保证不丢不重。

## 后端

### StreamBroker (`src/trilobite/broker.py`)

per-session 事件总线，维护：

* `_subscribers`：每个连接的 SSE 客户端对应一个 `asyncio.Queue`。
* `_turn_buffer`：当前 run 的事件（从第一个 `turn` 到现在），用于回放给中途连接的客户端。
* `_persisted_len`：已"提交"到 `init` 快照的历史长度。回放缓冲对应 `persisted_len` 之后的历史，二者不重叠。
* `_lock`：保证快照与回放的一致性。

关键方法：

* `publish(event, history_len)`：广播事件；`turn` 开始累积缓冲，`done/cancelled/error` 结束 run，把 `persisted_len` 推进到当前历史长度并清空缓冲。
* `commit(history_len)`：compaction 重写历史后调用，推进 `persisted_len` 并清空缓冲（避免重连时快照与回放重复）。
* `attach(...)`：新客户端订阅——回放缓冲到其队列 + 返回 `init` 快照，整个过程持锁。
* `detach(q)`：客户端断开时移除其队列。

### Agent (`src/trilobite/agent.py`)

* `start(message)` / `steer(message)`：async，发 `user` 事件后启动/转向 run。
* `run()`：不再接收 queue 参数，通过 `_send_stream_event` → `broker.publish` 广播；run 结束在 `finally` 中兜底清除 running 标志。
* `attach_subscriber()` / `detach_subscriber(q)`：供 `/stream` 端点使用。
* `is_running()`：基于 broker 状态（`start` 时即置 true，先于第一个 `turn` 事件）。

### 端点 (`src/trilobite/server.py`)

* `POST /api/sessions/{name}/message`：running 则 steer 返回 `{status:"steered"}`，否则 start 返回 `{status:"started"}`。**不返回流**。
* `GET /api/sessions/{name}/stream`：SSE 订阅。连接时发 `init`，随后持续推送事件，空闲时发 `: keepalive` 心跳；`done/cancelled/error` 后保持连接以等待下一个 run。

## SSE 事件协议

| 事件 | 字段 | 说明 |
|---|---|---|
| `init` | `history, is_running, token_count, max_context_tokens, plan_mode, additional_dirs` | 连接时首发，前端据此重建对话与状态 |
| `user` | `text` | 用户消息（start/steer 时发），前端据此渲染用户气泡 |
| `turn` | — | 一个 LLM 回合开始，置 `is_running=true` |
| `thinking` | `text` | 思考增量 |
| `text` | `text` | 正文增量 |
| `tool_stream` | `tool_name, args, complete` | 工具参数流式 |
| `tool_start` | `tool, args` | 工具开始执行 |
| `tool_result` | `tool, result, diff_prev?, diff_current?` | 工具结果 |
| `usage` | `token_count, max_context_tokens` | token 用量 |
| `status` | `text` | 状态横幅（如 compaction） |
| `plan_exit_request` | — | 请求退出 plan 模式 |
| `permission_request` | `path, tool, message` | 请求文件访问权限 |
| `done` | — | run 正常结束，置 `is_running=false` |
| `cancelled` | — | run 被取消，置 `is_running=false` |
| `error` | `text, status_code?, error_type?, error_code?` | run 出错，置 `is_running=false` |

## 重连 / 多开一致性

`persisted_len` 标记已进入 `init` 快照的历史；回放缓冲只含当前 run 未提交的事件：

* **运行中重连**：`init.history = history[:persisted_len]`（不含本 run）+ 回放缓冲（本 run 全部事件）→ 完整且不重叠。
* **空闲重连**：`persisted_len = len(history)`，缓冲为空，`init` 含全部历史。
* **compaction**：重写历史后 `commit` 推进 `persisted_len` 并清空缓冲。
* **done/cancelled**：agent 已把本 run 结果 append 到 history，`publish` 推进 `persisted_len` 并清空缓冲，避免与下一次 `init` 重复。

## 前端 (`frontend/src/store.ts`)

* `selectSession` → `connectStream`：建立 SSE 订阅（取代旧的 `loadHistory` + 请求式流）。
* `init` 事件重建 `chatItems` 与状态；`user` 事件渲染用户消息（前端不再乐观 push，保证重连一致）。
* `isStreaming` 由 `turn`/`done`/`cancelled`/`error` 事件驱动。
* `sendMessage` 只 POST，不处理流。
* 网络断开自动重连（1s 退避）；切换 session 主动 abort 旧连接。

### 工具结果展示（`ToolEntry.vue`）

* `read` 工具的结果默认折叠（`<details>` 收起），仅显示标签行 `[read: <filename>]`，点击展开查看完整输出，避免长文件内容刷屏。
* `bash`、`write` 等工具仍默认展开（`write` 走 diff 视图）。

### 思考展示（`ThinkingBlock.vue`）

* 默认只显示约 3 行高度，`overflow: hidden` 不可手动滚动；流式输出时通过 `transform` 把内容尾部对齐到窗口底部，始终显示最新的几行（类似 `tail -f`）。
* 切换按钮 `▸/▾` 置于块底部、紧跟最新内容，保证流式滚动时始终可见可点；点击展开显示全部内容，默认不展开。
