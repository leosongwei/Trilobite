# 对话历史

## 概述

对话历史是 agent 的记忆载体，由 `History` 类管理，持久化在 `~/.config/trilobite/sessions/<session_name>/history.json`。history 的第一条消息始终是 system 消息（详见 [context_building.md](./context_building.md)）。

历史内部以**有类型的消息对象**表示（`messages.py`），而非裸 dict。一条 `AssistantMessage` 是自包含的一轮 agent 动作：thinking + content + tool_calls + tool_results 都在一个对象里。这让用户在运行中追加的 steering 消息**永远落在整个 assistant turn 之后**，不可能插进 `assistant(tool_calls)` 和它的 tool results 之间，从根本上保证了发给 API 的消息序列永远合法。

## 消息对象模型

定义在 `src/trilobite/messages.py`：

```
SystemMessage(content)                      # system 消息（初始 prompt 或压缩后重建的 prompt）
CompactMarker()                             # 压缩边界：纯标记，不带内容；其后跟一条重建的 SystemMessage
UserMessage(content, compact_summary=False) # 用户输入（compact_summary 标记压缩摘要）
AssistantMessage(thinking, content, tool_calls, tool_results)  # 自包含的一轮
  ├─ ToolCall(id, name, arguments)          # arguments 流式追加
  └─ ToolResult(tool_call_id, content, diff) # diff 仅 edit 工具，仅供前端
```

每个对象有三种投影：

| 方法 | 用途 |
|------|------|
| `to_api_dicts()` | 展开成 OpenAI 兼容 dict 列表发给 LLM。`AssistantMessage` 展开为 `[assistant, tool, tool, ...]`，`CompactMarker` 展开为空（纯裁剪标记）。**diff 不发给 API**。 |
| `to_storage_dict()` | v2 JSON 形状，持久化到 history.json |
| `to_frontend_dicts()` | 扁平的 v1 兼容 dict 列表，用于 `init` SSE 快照（前端协议不变） |

`AssistantMessage` 的 assistant dict 形状遵循旧约定：带 `tool_calls` 时 `content`/`reasoning_content` 可选；纯文本 turn 时 `content` 始终存在（即使空串）。

## 持久化：v2 格式与 v1 兼容

`history.json` 是 v2 格式，带版本号：

```json
{
  "version": 2,
  "messages": [
    { "type": "system", "content": "..." },
    { "type": "user", "content": "帮我读一下 main.py" },
    { "type": "assistant", "thinking": "...", "content": "让我先读...",
      "tool_calls": [{ "id": "call_1", "name": "read", "arguments": "{\"filename\":\"main.py\"}" }],
      "tool_results": [{ "tool_call_id": "call_1", "content": "1: ...", "diff": null }] },
    { "type": "compact_marker" },
    { "type": "system", "content": "重建的 system prompt" },
    { "type": "user", "content": "<compact>...</compact>", "compact_summary": true }
  ]
}
```

**v1 兼容**：旧 session 的 `history.json` 是裸 dict 数组（无版本号）。`History._load` 检测顶层类型——是 list 即按 v1 加载：`from_v1()` 遍历扁平数组，把 `assistant(tool_calls)` + 紧跟的连续 `tool` 消息**合并**成一个自包含 `AssistantMessage`（`reasoning_content` 映射为 `thinking`）。加载失败会记录日志而非静默吞掉。

**惰性升级**：任何 `save()` 都写 v2 格式。所以一个 v1 session 一旦被新代码读写就自动转成 v2，不需要批量迁移脚本。

## History 类

`src/trilobite/history.py` 的 `History` 封装历史的读取、保存和 API 投影：

| 方法 | 说明 |
|------|------|
| `append(msg, persist=True)` | 追加一条消息。`persist=False` 用于 drain 开始时入列的空 `AssistantMessage`（见下文） |
| `extend(msgs)` / `insert(i, msg)` | 批量追加 / 指定位置插入 |
| `pop()` | 弹出末尾消息（中断丢弃空 asst 时用） |
| `truncate(index)` | 丢弃从 `index` 开始的所有消息（revert 用） |
| `save()` | 显式全量写盘（mutate 消息对象后调用） |
| `get_api_messages()` | 返回 API 投影：从最后一个 `CompactMarker` 之后开始，连续 user 用 `combine_new_messages` 合并 |
| `to_flat_dicts()` | 展开成扁平 v1 兼容 dict 列表（前端 `init` 快照用） |
| `raw` | 原始对象列表（compaction、revert 等遍历用） |

### 存储与投影的分离

消息在 history.json 中以对象化结构存储，保留原始边界。发送给 API 时，`get_api_messages()` 从最后一个 `CompactMarker` **之后**开始投影（marker 自身不带内容、不投影；紧跟其后的 `SystemMessage` 成为新 system 消息），marker 之前的消息从 API 上下文丢弃但仍保留在持久化历史里供前端查看。连续的 user 消息由 `combine_new_messages` 合并成一条：单条原样透传，多条用 `<multi_message/>` 分隔，让模型知道这是多条独立用户输入（如多条 steering，或 steering + 压缩指令）：

```
history:  UserMessage("用 Python"), UserMessage("另外加上日志")
API 收到:  {role: user, content: "<multi_message/>\n用 Python\n<multi_message/>\n另外加上日志"}
```

系统提示词里说明了 `<multi_message/>` 标记的含义。`CompactMarker` 不投影成 system（它无内容），重建的 `SystemMessage` 才是 API 上下文的新 system 起点。

## 流式与控制流

### drain 即 append：消息对象是流式输出的一等公民

每轮 turn **一开始**就 append 一个空的 `AssistantMessage`（`persist=False`），drain 流式增量直接 mutate 这个对象（`asst.thinking += delta`、`asst.content += delta`、`asst.tool_calls.append(...)`），并经事件流发给前端。不再有 `content_parts`/`thinking_parts` 累加器。

空对象 `persist=False` 入列（不入盘），只在定稿后 `save()`：纯文本 turn 在 drain 完后 save；带工具的 turn 在 `tool_calls` 落定后 save 一次，每填一个 `tool_result` 再 save 一次。这样崩溃在 drain 中途不会留下半条记录；崩溃在工具之间则留下一个可被 `_patch_dangling_tool_calls` 补全的记录。

### 续跑判断：何时结束 run

`run()` 主循环每轮 turn 之前做续跑判断——只在「有新内容要模型响应」时才跑下一轮，否则结束 run：

```
has_unread_user = _count_user_messages() > _user_read_cursor
if not (_pending_tool_results or has_unread_user or _force_run):
    break   # run 结束，发 done
```

三个续跑信号：

* `_pending_tool_results` —— 上一轮产出了 `tool_calls`，模型还没看到 tool results（自包含 turn 里的 `tool_results`）。决定跑一轮后即清零，所以纯文本 turn 不会因此无限续跑。
* `has_unread_user` —— 自模型上次读取（`get_api_messages` 调用时刻，记录在 `_user_read_cursor`）后有新的 user 消息（start/steer）。
* `_force_run` —— 压缩后强制跑一轮，让模型在重建的上下文上继续。

`_user_read_cursor` 在 `get_api_messages()` 调用**之后**（drain 之前）更新，记录这一轮模型实际读到的 user 消息数。这样 drain 中途到达的 steer 落在 cursor 之后，驱动下一轮续跑。

### done 的时机

纯文本 turn（无 tool_calls）不再立即发 `done` 并 break。它先 persist、标记 `completed`，然后回到循环顶部做续跑判断——如果 drain 期间来了 steer，`has_unread_user` 为真就再跑一轮响应它；否则才 break，循环退出后发 `done`。这修复了旧设计中「纯文本最终回复期间 steer 会滞留队列、要等下一次 run 才被看到」的问题。

## Steering（运行中追加消息）

steer 不再经过队列。`steer()` 直接 append 一个 `UserMessage` 到 history 并发 `user` 事件。由于 steer 只在 `is_running()` 时被调用（停机时走 `start`），run 循环正活着，下一个续跑判断会检测到这条未读消息并跑一轮让模型响应。

因为 `AssistantMessage` 自包含 `tool_results`，steer 的 `UserMessage` 物理上只能落在整个 assistant turn 之后，永远不可能插进 `assistant(tool_calls)` 与其 tool results 之间——这是对象化设计的核心收益。

发送给 API 时，连续的 user 消息（包括多条 steer）由 `get_api_messages()` 合并：

```
history:  ... assistant(tool_calls, tool_results), UserMessage("用 Python"), UserMessage("另外加上日志")
API 收到: ... assistant, tool, tool, {user: "用 Python\n\n另外加上日志"}
```

## 压缩（compaction）

压缩完全统一进主循环，没有专门的 compact 方法。流程：

1. **触发**：一轮（含工具执行）结束后读 token 用量，若超过阈值，标记 `_need_compact=True` 并把压缩指令（`build_compact_prompt`）作为一条普通 `UserMessage` append 进 history。它和任何未读的 steering 消息一样，是 user 消息——不区分「正常 prompt」和「steering prompt」。
2. **压缩 turn**：下一轮续跑判断发现有新 user 消息（压缩指令），照常跑一轮；但因为 `_need_compact=True`，`tools=None`（关闭工具），模型只能产出文本 handoff note。这轮的 `get_api_messages` 会把 steering + 压缩指令用 `combine_new_messages` 合并成一条 user，模型一次性读到全部并写进 note。
3. **重建**：压缩 turn（纯文本）结束后，`_finalize_compaction` 落盘一个无内容的 `CompactMarker`、一条重建的 `SystemMessage`、以及把 note 包成 `<compact>...</compact>` 的 `compact_summary` user 消息。`get_api_messages` 从 marker 之后开始，所以压缩前的全部（steering、压缩指令、note turn）从 API 上下文丢弃，但留在持久化历史里。重置 token 计数，设 `_force_run`。
4. **继续**：`_force_run` 让主循环在重建的上下文上再跑一轮，模型基于 note 继续。

steering 不需要任何特殊处理：它在压缩 turn 被模型读到并写进 note，原文随 marker 裁剪——这正是压缩对所有上下文做的事。`COMPACTION_PROMPT` 已相应改写，告诉模型下一轮只会看到 note（不再有「最近的 user 消息会保留」的预期），需把未处理的用户请求写进 note。

手动 `/compact`（`compact_now`）走同一条路：设 `_need_compact` + append 压缩指令 + 启动 run，剩余由主循环处理。详见 [compact.md](./compact.md)。

## 保存时机

| 时机 | 说明 |
|------|------|
| `start()` / `steer()` | append `UserMessage`（立即 save） |
| drain 开始 | append 空 `AssistantMessage`（`persist=False`，不入盘） |
| 纯文本 turn drain 完 | `save()` 定稿 |
| 带 tool_calls turn drain 完 | `save()`（含 tool_calls，便于崩溃补全） |
| 每个工具执行完 | 填 `tool_result` 后 `save()` |
| 取消时保留部分输出 | `save()` 落盘 in-flight 的 `AssistantMessage`（有内容时）；空对象则 `pop()` 丢弃 |
| 压缩 | 各步 append 后 save |

## 编辑重发（revert）

用户可以编辑之前发送的某条消息并从该处重新推理（`POST /api/sessions/{id}/revert`，参数 `user_seq` + `message`）。`Agent.revert` 按该消息是否已被模型读取分两种处理：

* **已被模型读取**（`user_seq < _user_read_cursor`）：若正在运行先 `stop()`，用 `history.truncate(target)` 丢弃该 user 消息及其后所有内容，`broker.commit(target)` 重置回放基准，再 `start(message)` 重新推理。端点返回 `rerun`，前端重连 SSE。
* **尚未被读取**（steer 还在 history 里、模型未读到，`user_seq >= _user_read_cursor`）：直接改 history 中该 `UserMessage.content`，**不中断运行**，广播 `user_edit` 事件让前端就地更新。端点返回 `queued`，前端无需重连。

`user_seq` 计数排除 `compact_summary`（与 `_count_user_messages` 一致），定位 target 时同样排除，避免了旧设计中两处计数基准不一致的 bug。

## 前端协议不变

前端拿到的是 `init` 快照里的扁平 `HistoryMessage[]`（`AssistantMessage` 展开成 `assistant` + 多条 `tool`）。字段名（`reasoning_content`、`compact_marker`、`compact_summary`、`diff`、`tool_calls`、`tool_call_id`）与 v1 完全一致，`parseHistory` 无需改动。对象化是后端内部重构。

## broker 回放

`StreamBroker` 的 `persisted_len` 语义不变：仍按 run 边界推进（仅终态 `done`/`cancelled`/`error`/`interrupted` 及压缩后 `commit` 推进），run 期间 append/mutate 的对象都在 `persisted_len` 之后，由 `_turn_buffer` 的事件流镜像覆盖。`attach` 切片 `raw[:persisted_len]`（对象列表），由 `attach_subscriber` 展开成扁平 dict 放入 `init` 快照。详见 [streaming.md](./streaming.md)。

## 会话命名冲突处理

创建新 session 时，如果指定的名称已存在，后端会自动追加数字后缀避免冲突：

```
foo        -> 首次创建，使用原名
foo        -> 再次创建，自动变为 foo(2)
foo        -> 再次创建，自动变为 foo(3)
foo(2)     -> 再次创建，自动变为 foo(4)
```

前端 `createSession` 会接收并采用后端返回的实际名称，确保 UI 显示与持久化一致。
