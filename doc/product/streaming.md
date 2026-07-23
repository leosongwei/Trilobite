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
* 工具执行不阻塞事件循环：`execute_tool` 仍是同步函数，但 `run()` 在调用处用 `asyncio.to_thread(...)` 把它丢到工作线程执行。所有 agent / subagent 共享同一个事件循环，若让阻塞调用直接在循环里跑，长 bash 命令会冻住整个循环——SSE 心跳发不出、新连接连 `init` 都拿不到、subagent 跑 bash 时主界面整体卡死（issue #5）。丢线程后循环保持响应；`task` 仍走 `await self._run_subagents`，`execute_tool` 本身不异步化。bash 内部改用 `Popen`+`communicate`（`start_new_session=True`）并经 `on_proc` 把进程句柄回注册到 agent，`interrupt()` 杀整个进程组让长命令立即返回再走总结（详见 subagent.md 的「bash 中断」）。
* `attach_subscriber()` / `detach_subscriber(q)`：供 `/stream` 端点使用。
* `is_running()`：基于 broker 状态（`start` 时即置 true，先于第一个 `turn` 事件）。

### 端点 (`src/trilobite/server.py`)

* `POST /api/sessions/{name}/message`：running 则 steer 返回 `{status:"steered"}`，否则 start 返回 `{status:"started"}`。**不返回流**。
* `GET /api/sessions/{name}/stream`：SSE 订阅。连接时发 `init`，随后持续推送事件，空闲时发 `: keepalive` 心跳；`done/cancelled/error` 后保持连接以等待下一个 run。

## SSE 事件协议

| 事件 | 字段 | 说明 |
|---|---|---|
| `init` | `history, is_running, token_count, max_context_tokens, plan_mode, additional_dirs` | 连接时首发，前端据此重建对话与状态 |
| `user` | `text, user_seq` | 用户消息（start/steer 时发），前端据此渲染用户气泡；`user_seq` 为该消息在所有 user 消息中的序号 |
| `user_edit` | `user_seq, text` | revert 编辑队列中尚未被模型读取的 steer 消息时发，前端就地更新对应 user 气泡 |
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
* `revert(userSeq, message)`：编辑历史用户消息并重发。返回 `rerun` 则重连 SSE 重建对话；返回 `queued` 则由 `user_edit` 事件就地更新气泡，不重连（详见 [history.md](./history.md)）。
* 网络断开自动重连（1s 退避）；切换 session 主动 abort 旧连接。

### 自适应加载（`ChatView.vue`）

切到历史很长的 session 时，一次性渲染全部 `chatItems` 会让浏览器卡顿很久，因此 `ChatView` 对消息列表做**窗口化**：

* 只渲染靠近底部的 `INITIAL_VISIBLE`（10）条消息；`visibleItems = chatItems.slice(windowStart)`，`windowStart = length - effectiveRender`。
* 用户滚到顶部（`scrollTop <= TOP_THRESHOLD`）时向上扩窗 `LOAD_MORE`（10）条，扩窗前后用 `scrollHeight` 差值恢复 `scrollTop`，保持视觉位置不跳。顶部有"滚动到顶部加载更早的消息…"提示。
* 若可见内容比视口还短却仍有更早消息（极短消息场景），`fillViewport` 自动扩窗直到填满视口，避免出现无法滚动加载的空白死区；仅在非流式时运行。
* 流式输出持续向底部追加，窗口始终包含末尾，所以最新内容永远可见；`streamTick`/`chatItems.length` 的 watcher 继续把视图钉在底部，行为与窗口化前一致。
* 切 session 时 `renderCount` 重置回 `INITIAL_VISIBLE`；窗口扩大引入新 DOM 节点后同样触发 MathJax typeset。

### 工具结果展示（`ToolEntry.vue`）

* `read` 工具的结果默认折叠（`<details>` 收起），仅显示标签行 `[read: <filename>]`，点击展开查看完整输出，避免长文件内容刷屏。
* `bash`、`write`、`edit` 等工具仍默认展开（`edit` 走 diff 视图，`write` 走文本结果）。

### 思考展示（`ThinkingBlock.vue`）

### 思考展示（`ThinkingBlock.vue`）

* 默认折叠只显示约 3 行高度，`overflow: hidden` 不可手动滚动；点击展开显示全部内容，默认不展开。切换按钮 `▸/▴` 置于块底部、紧跟最新内容，保证流式滚动时始终可见可点。
* **"活的" thinking 泡泡**（`ChatView` 的 `liveIdx`：最后一个 chatItem 是 turn、`thinking` 非空、`text` 与 `tools` 均空）保留全文，流式输出时通过 `transform` 把内容尾部对齐到窗口底部，始终显示最新的几行（类似 `tail -f`）。
* 一旦某个 thinking 泡泡下方出现任何内容（同 turn 的正文/工具调用，或下一个 turn、用户消息），它就从 live 变成"老"泡泡：自动折叠、清掉 transform，折叠时**只把最后 3 行（超长单行按字符兜底截取）写进 DOM**（`displayContent` 截取尾部预览），不渲染整段思考；展开后才渲染全文。这样长对话里已完成的 thinking 不再各自携带全文 DOM，避免越堆越卡；切 tab 由历史重建时也只有最底部那个（若仍在思考）带全文。
