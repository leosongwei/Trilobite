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
* `_turn_buffer`：当前 run 的事件（从第一个 `turn` 到现在），用于回放给中途连接的客户端。缓冲以**合并形态**保存：相邻的同类增量事件折叠为一条（thinking/text 增量拼接、同一 tool_call 的 tool_output 行按 `\n` 连接、同一工具的 tool_stream 参数保留累计值），输出量大的 run 也只回放几十条事件而非数万条原始增量——中途连接的客户端瞬间追平，不会逐条处理 live delta 而卡死；live 订阅者仍收到原始逐条事件。工具结束时其 tool_output 条目从缓冲移除（最终结果已进 `tool_result`）。
* `_persisted_len`：已"提交"到 `init` 快照的历史长度。回放缓冲对应 `persisted_len` 之后的历史，二者不重叠。
* `_lock`：保证快照与回放的一致性。

关键方法：

* `publish(event, history_len)`：广播事件；`turn` 开始累积缓冲，`done/cancelled/error` 结束 run，把 `persisted_len` 推进到当前历史长度并清空缓冲。
* `commit(history_len)`：compaction 重写历史后调用，推进 `persisted_len` 并清空缓冲（避免重连时快照与回放重复）。
* `attach(...)`：新客户端订阅--回放缓冲到其队列 + 返回 `init` 快照，整个过程持锁。
* `detach(q)`：客户端断开时移除其队列。

### Agent (`src/trilobite/agent.py`)

* `start(message)` / `steer(message)`：async，都 append 一条 user 消息并发 `user` 事件。`start` 停机时调用，启动新 run task；`steer` 仅 run 进行中调用，消息直接落 history，run 循环在下一个 turn 边界（续跑判断）拾取。
* `run()`：不再接收 queue 参数，通过 `_send_stream_event` → `broker.publish` 广播；run 结束在 `finally` 中兜底清除 running 标志。
* **流式回合统一重试**：任意 HTTP 错误、流未正常结束（无 `[DONE]`/finish reason）或工具调用回合未输出正文，都走同一重试循环（`_stream_turn`）——丢弃本回合部分输出、重发相同请求，每次重试广播 `status` 横幅 + `turn_restart` 事件，上限 `max_stream_retries`（默认 10，含首次）。详见 [llm_transport.md](./llm_transport.md)。
* 工具执行不阻塞事件循环：`execute_tool` 仍是同步函数，但 `run()` 在调用处用 `asyncio.to_thread(...)` 把它丢到工作线程执行。所有 agent / subagent 共享同一个事件循环，若让阻塞调用直接在循环里跑，长 bash 命令会冻住整个循环--SSE 心跳发不出、新连接连 `init` 都拿不到、subagent 跑 bash 时主界面整体卡死（issue #5）。丢线程后循环保持响应；`task` 仍走 `await self._run_subagents`，`execute_tool` 本身不异步化。bash 内部用 `Popen`（`start_new_session=True`）启动命令，两个读取线程逐行 drain stdout/stderr，每行经 `on_output` 回调转发为 `tool_output` 事件流式推送到前端（工作线程通过 `asyncio.run_coroutine_threadsafe` 把事件调度回事件循环）；同时收集所有行用于最终拼接 `[stderr]`/`[exit code]` 标记并按 `max_output_lines`/`max_output_chars` 截断后作为 `tool_result` 返回。进程句柄经 `on_proc` 回注册到 agent，`interrupt()` 杀整个进程组让长命令立即返回再走总结（详见 subagent.md 的「bash 中断」）。
* `attach_subscriber()` / `detach_subscriber(q)`：供 `/stream` 端点使用。
* `is_running()`：基于 broker 状态（`start` 时即置 true，先于第一个 `turn` 事件）。

### 端点 (`src/trilobite/server.py`)

* `POST /api/sessions/{id}/message`：running 则 steer 返回 `{status:"steered"}`，否则 start 返回 `{status:"started"}`。**不返回流**。
* `GET /api/sessions/{id}/stream`：SSE 订阅。连接时发 `init`，随后持续推送事件，空闲时发 `: keepalive` 心跳；`done/cancelled/error` 后保持连接以等待下一个 run。响应带 `Cache-Control: no-store`（所有 `/api/*` 响应均禁止缓存，避免浏览器启发式缓存中断的流）。

## SSE 事件协议

| 事件 | 字段 | 说明 |
|---|---|---|
| `init` | `history, is_running, token_count, max_context_tokens, plan_mode, additional_dirs` | 连接时首发，前端据此重建对话与状态 |
| `user` | `id, text, user_seq` | 用户消息（start/steer 时发），前端据此渲染用户气泡；`id` 为该消息的消息 id（revert 用），`user_seq` 为在真实 user 消息中的序号 |
| `user_edit` | `message_id, text` | revert 编辑尚未被模型读取的 steer 消息时发（消息已在 history 但模型未读到），前端按 `message_id` 就地更新对应 user 气泡 |
| `turn` | -- | 一个 LLM 回合开始，置 `is_running=true` |
| `turn_restart` | -- | 流式回合失败重试：丢弃本回合已流出的部分输出（坏思维链/截断正文/工具片段），开启新的回合泡泡，`is_running` 不变 |
| `thinking` | `text` | 思考增量 |
| `text` | `text` | 正文增量 |
| `tool_stream` | `tool_name, args, complete` | 工具参数流式 |
| `tool_start` | `tool, args, tool_call_id` | 工具开始执行；`tool_call_id` 关联流式输出与结果 |
| `tool_output` | `tool_call_id, stream, text` | bash 工具的实时输出行（`stream` 为 `stdout`/`stderr`），前端按 `tool_call_id` 追加到对应工具的输出窗口；仅用于实时观察，不写入 history |
| `tool_result` | `tool, result, diff?` | 工具结果（`result` 经 `max_output_lines`/`max_output_chars` 截断后写入 history；`diff` 为 edit 的结构化行级 diff：`[{type, old, new, text}]`，带真实文件行号） |
| `usage` | `token_count, max_context_tokens` | token 用量 |
| `status` | `text` | 状态横幅（如 compaction） |
| `plan_exit_request` | -- | 请求退出 plan 模式 |
| `permission_request` | `path, tool, message` | 请求文件访问权限 |
| `done` | -- | run 正常结束，置 `is_running=false` |
| `cancelled` | -- | run 被取消，置 `is_running=false` |
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
* `revert(messageId, message)`：编辑历史用户消息并重发。返回 `rerun` 则重连 SSE 重建对话；返回 `queued` 则由 `user_edit` 事件就地更新气泡，不重连（详见 [history.md](./history.md)）。
* 网络断开自动重连（1s 退避）；切换 session 主动 abort 旧连接。
* 每个流连接使用**不同的 URL**（`/stream?_t=<随机值>`）：Firefox 对同一 URL 的在途 GET 做 single-flight 合并——第二个请求会等第一个响应结束（SSE 永不结束，于是第二个 tab 打开同一 session 时 `/stream` 在浏览器缓存层无限排队，表现为"加载不出来"）。随机 query 让每个连接走独立的缓存槽，多开/重连互不阻塞。

### 自适应加载（`ChatView.vue`）

切到历史很长的 session 时，一次性渲染全部 `chatItems` 会让浏览器卡顿很久，因此 `ChatView` 对消息列表做**窗口化**（防止 DOM 膨胀）：

* 窗口由两个边界定义：`visibleItems = chatItems.slice(windowStart, windowEnd)`（半开区间）。窗口通常包含末尾（`windowEnd === chatItems.length`），流式输出向底部追加时窗口跟着增长；用户滚回底部时窗口重新包含末尾。v-for 以条目对象为 key，扩窗/卸载只挂载/卸载变化的条目，不复用整窗重挂载。
* 初始只渲染底部 `INITIAL_VISIBLE`（10）条；`fillViewport` 以 `FILL_STEP`（2）条为步长向上增量扩窗，每步钉底并检查视口，直到内容溢出视口（只加载足够填满页面的数量）或达到条数硬上限 `MAX_FILL`（30）——硬上限不依赖 `scrollHeight` 测量，滚动容器测量一旦异常（高度不受约束/内容高度未更新）也不会把整个历史一次性挂进 DOM；内容不足一屏的短 session 则一直扩到全部加载。视口已满时 `fillViewport` 直接返回（初始 10 条已填满视口就不自动扩窗，往上滚动才加载更早的；不打扰正在翻历史的用户），只在用户原本钉底时扩窗后重新钉底。run 结束（`isStreaming` 翻回 false）时若内容仍不足一屏也会补齐。
* 用户滚到顶部（`scrollTop <= TOP_THRESHOLD`）时向上扩窗 `LOAD_MORE`（10）条，扩窗前后用 `scrollHeight` 差值恢复 `scrollTop`，保持视觉位置不跳。顶部有"滚动到顶部加载更早的消息…"提示。
* 窗口有上限 `MAX_VISIBLE`（40）条：超出后从**底部**卸载条目（`trimExcess`，每批最多 `LOAD_MORE` 条）——只卸载已完全滚出视口下方的条目（按 `getBoundingClientRect` 判断，含安全余量），用户向上翻历史加载的旧消息位于窗口顶部，保留不卸载，可以一路翻到最早。卸载底部内容不影响当前视口，无需滚动补偿；流式输出期间和用户钉底时不卸载（避免卸掉正在输出的泡泡或正在看的短消息）。滚回底部附近（`BOTTOM_THRESHOLD`）时窗口重置回底部 `INITIAL_VISIBLE` 条并重新钉底。
* 滚动钉底（`scrollToBottom`）的触发时机：
  * **顶层 item 追加**（turn / user / compact / error）：`chatItems.length` watcher，窗口包含末尾时新消息进入窗口，用户钉底时 `nextTick` 后滚到底；翻历史（窗口不含末尾）时新消息不进入窗口、不滚动。
  * **turn 内部新泡泡首次出现**（thinking 从空变非空、正文从空变非空、新增工具调用）：`bubbleCount` watcher 滚一次底（仅当窗口包含末尾且用户钉底时）。
  * **thinking 折叠框增高（封顶前）**：`streamTick` watcher（`maybeScrollThinking`）测量底部 live 泡泡 `.thinking-body` 的 `clientHeight`，每次高度增长滚一次底，封顶（max-height）后不再滚动（详见下节思考展示）。
  * **run 结束**：`isStreaming` 翻回 `false`（done / cancelled / interrupted / error，content 输出完毕）时滚一次底，展示完整输出（仅当窗口包含末尾且用户钉底时）。
  * **流式增长不触发滚动**：thinking / text / tool_output 的每个 delta（`streamTick`）不钉底，用户可自由往上翻看历史。
* 切 session 时重置窗口状态（回底部 `INITIAL_VISIBLE` 条、thinking 滚动高度记录清零）；窗口扩大引入新 DOM 节点后同样触发 MathJax typeset。

### Markdown 与公式渲染（`TurnBlock.vue` / `mathjax.ts`）

只有 assistant 正文走 `renderMarkdown` → `v-html` → MathJax 的完整链路：

* `renderMarkdown`（`markdown.ts`）先把 `$$…$$`/`$…$` 提取成占位符保护起来，`marked` 转完 HTML 再还原成 MathJax 风格的 `\[…\]`/`\(…\)`，避免被 marked 破坏。
* MathJax（`mathjax.ts` 的 `typesetMath`）在已渲染的 DOM 上把分隔符渲染成 CHTML 公式。**作用域只限 `.markdown-body` 元素**（即 assistant 正文），由 `ChatView` 在每次 typeset 前用 `querySelectorAll('.markdown-body')` 收集目标传入，而**不是**整个 `chat` 容器。
* 之所以收窄作用域：工具调用小标题（`[bash: echo $HOME]`、`[grep: foo$]`）、思考块、用户消息、diff 文本都是纯文本插值（`{{ }}`，不走 markdown），其中的 `$` 若被 MathJax 扫到会被误判为内联公式分隔符而错乱渲染。收窄到正文后这些区域不再被数学化。
* 流式输出时 `v-html` 会随每个 delta 重设 `innerHTML` 覆盖掉刚渲染的 `<mjx-container>`，而 MathJax 内部仍保留旧节点状态导致后续 typeset 静默失败（公式"闪一下就消失"），因此流式期间（`isStreaming`）跳过 typeset，等流结束再统一渲染。

### 工具结果展示（`ToolEntry.vue`）

* `read` 工具的结果默认折叠（`<details>` 收起），仅显示标签行 `[read: <filename>]`，点击展开查看完整输出，避免长文件内容刷屏。
* `bash`、`write`、`edit` 等工具仍默认展开（`edit` 走 diff 视图，`write` 走文本结果）。

### 思考展示（`ThinkingBlock.vue`）

* 默认折叠：body 有 `max-height: 4.5em`（`overflow: hidden`）上限，但框高度随内容**渐进增长**——内容 1 行时框 1 行高、2 行时 2 行高，……直到封顶于 max-height 后不再变高。切换按钮 `▾/▸` 置于块顶部（内容向下展开），点击展开显示全文。
* **滚动钉底**（触发时机总纲见上节自适应加载）：thinking 泡泡首次出现由 `bubbleCount` watcher 滚动一次；流式增长期间，`ChatView` 的 `streamTick` watcher（`maybeScrollThinking`）测量底部泡泡 `.thinking-body` 的 `clientHeight`，**框高度每次增长（封顶前）都滚到底**（同一行内增长不重复滚），封顶后高度不再变化、停止滚动——一两行的短泡泡始终完整可见，长思考不打扰用户翻看历史。用户手动展开（`.expanded`）后不自动滚动。
* 内容超出折叠高度后：**"活的"泡泡**（`ChatView` 的 `liveIdx`：最后一个 chatItem 是 turn、`thinking` 非空、`text` 与 `tools` 均空）保留全文，用 `transform` 把内容尾部对齐到框底（类似 `tail -f`），最新几行始终可见；**"老"泡泡**（下方已出现正文/工具/后续内容）折叠时只把最后 3 行（超长单行按字符兜底截取）写进 DOM（`displayContent` 截取尾部预览），不渲染整段思考，避免长对话里已完成的 thinking 各自携带全文 DOM。
* 泡泡从 live 变成"老"（下方出现同 turn 的正文/工具调用，或下一个 turn、用户消息）时**保持当前展开/折叠状态**，只清掉手动折叠可能残留的 transform。
