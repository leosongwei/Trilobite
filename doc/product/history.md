# 对话历史

## 概述

对话历史是 agent 的记忆载体，由 `MessageList` 类管理，持久化在 `~/.config/trilobite/sessions/<session_name>/history.json`。history 的第一条消息始终是 system 消息（详见 [context_building.md](./context_building.md)）。

历史内部是一个**扁平的 typed 消息列表**（`messages.py`），其磁盘形式（v3）与 OpenAI completions API 的消息序列**逐字段同构**：每一条目带 `type` 字段（`system`/`user`/`model`/`tool_results`/`compact_marker`）和 `id` 字段（消息 id，revert 按 id 定位）。一条 model turn 在列表里拆成两条条目：`ModelMessage`（think/content/calls）后紧跟它的 `ToolResults`（该批 calls 的结果）。**顺序不变量**——同一批 calls 的所有结果连续位于任何后续 user 消息之前——由 `MessageList` 的 append 原语结构性保证，而不是靠对象嵌套。

## 消息对象模型

定义在 `src/trilobite/messages.py`：

```
Message(_id)                                # 基类：每个消息带紧凑 id（uuid4 hex 前 12 位）
SystemMessage(content)                      # system 消息（初始 prompt 或压缩后重建的 prompt）
CompactMarker()                             # 压缩边界：纯标记，不带内容；其后跟一条重建的 SystemMessage
UserMessage(content, compact_summary=False, is_compact_prompt=False, is_mode_notification=False, images=[]) # 用户输入
  └─ Image(filename, mime_type, original_name)  # 图片附件：文件存于 sessions/<id>/images/，历史里只存元数据
ModelMessage(think, content, tool_calls)    # 一次模型响应；think 为思维链（API 里叫 reasoning_content）
  └─ ToolCall(id, name, arguments)          # 一次工具调用；id 是 API 生成的 call_xxx（区别于消息 _id）
ToolResults(results)                        # 一批工具调用的结果，紧跟其 ModelMessage
  └─ ToolResult(tool_call_id, content, diff) # diff 仅 edit 工具、仅供前端
```

`Message._id` 在对象构造时生成、随持久化落盘，是**消息寻址**的唯一手段（revert 用它定位目标 user 消息），与 `ToolCall.id`（API 的 `call_xxx`，被 `ToolResult.tool_call_id` 引用）是两套 id。迁移自 v1/v2 的旧消息在 load 时补发 id。

每个对象有三种投影：

| 方法 | 用途 |
|------|------|
| `to_api_dicts()` | OpenAI 兼容 dict 发给 LLM。`ModelMessage` 投影为一条 `assistant`；`ToolResults` 投影为每条结果一条 `tool` |
| `to_storage_dict()` | v3 JSON 形状，持久化到 history.json（tool_calls 存 OpenAI 形状） |
| `to_frontend_dicts()` | 扁平 role-based dict 列表，用于 `init` SSE 快照和 `/history` 端点（前端协议不变） |

`ModelMessage` 的 assistant dict 遵循与旧版相同的 API 约定：带 `tool_calls` 时 `content`/`reasoning_content` 可选；纯文本 turn 时 `content` 始终存在（即使空串）。思维链字段在存储里叫 `think`，API/前端叫 `reasoning_content`，由投影负责映射。

## 持久化：v3 格式与迁移

`history.json` 是 v3 格式，带版本号：

```json
{
  "version": 3,
  "messages": [
    { "type": "system", "id": "a1b2c3d4e5f6", "content": "..." },
    { "type": "user", "id": "b2c3d4e5f6a7", "content": "帮我读一下 main.py" },
    { "type": "model", "id": "c3d4e5f6a7b8", "think": "让我先读...", "content": "",
      "tool_calls": [{ "id": "call_1", "type": "function",
                       "function": { "name": "read", "arguments": "{\"filename\":\"main.py\"}" } }] },
    { "type": "tool_results", "id": "d4e5f6a7b8c9",
      "results": [{ "tool_call_id": "call_1", "content": "1: ...", "diff": null }] },
    { "type": "user", "id": "e5f6a7b8c9d0", "content": "另外加一行日志" },
    { "type": "model", "id": "f6a7b8c9d0e1", "think": "用户要加日志", "content": "已加好日志" },
    { "type": "compact_marker", "id": "a7b8c9d0e1f2" },
    { "type": "system", "id": "b8c9d0e1f2a3", "content": "重建的 system prompt" },
    { "type": "user", "id": "c9d0e1f2a3b4", "content": "<compact>...</compact>", "compact_summary": true }
  ]
}
```

注意磁盘顺序**本身就是** API 合法的消息序列：`model(tool_calls)` 后紧跟它的 `tool_results` 整块，`user`（含 steering）只能出现在整块之后。v2 靠"结果嵌套在 assistant 对象里"容忍乱序；v3 扁平化后文件顺序必须即合法顺序，这一要求由 `MessageList` 的写入原语保证。

**惰性升级**：`MessageList._load` 按顶层形状分发——list 即 v1（裸扁平数组，`from_v1` 折叠 tool dict）、`version == 2` 即 v2（`from_v2` 展开嵌套的 `assistant.tool_results`）、`version == 3` 即 v3（逐条 `message_from_storage`）。三个 loader 产出同一种内存形态（扁平 typed 列表），并为每条旧消息补发 `_id`。任何 `save()` 都写 v3，旧 session 一旦被新代码读写就自动转成 v3，不需要批量迁移脚本。迁移是逐条独立的：没有磁盘索引跨越消息边界（`persisted_len`/`_token_covered` 是内存条目计数，与磁盘条目数无关）；load 时不做过度校验，遗留的开放 turn（有 calls 没结果）由运行前的 `_patch_dangling_tool_calls` 补全。

## MessageList：扁平事实源

`src/trilobite/history.py` 的 `MessageList` 是历史唯一的写入入口。它维护一个小的内部状态（"开放模型"位置），把**顺序不变量**封装进语义原语，调用者永远不用算位置：

| 方法 | 说明 |
|------|------|
| `append(msg, persist=True)` | 追加到末尾：user、system、marker、摘要等（这些放在末尾总是合法的） |
| `append_model(model, persist=False)` | turn 开始：追加空 model 壳（drain 前不入盘，定稿才 save） |
| `close_model()` | turn 定稿：关闭开放模型，之后不能再插入结果 |
| `insert_result(tr, after=None)` | 把一条结果插进开放模型的 `ToolResults` 条目（没有则在该模型后**新建**，位于任何后续 user 之前）；`after` 可指定其他模型 |
| `insert_results(results, after=None)` | 批量版本（dangling 补全用，一次 save） |
| `tool_results_of(model)` | 某模型紧跟的 `ToolResults` 条目（可能没有） |
| `truncate(index)` / `truncate_at(message_id)` | 丢弃从某位置/某 id 起的所有消息；`truncate_at` 返回保留长度（revert 用） |
| `index_of` / `get_by_id` / `user_seq_of` | 按 id 寻址；`user_seq_of` 计算真实 user 的序号（排除摘要/模式通知） |
| `save()` | 全量写 v3 |
| `get_api_messages(image_dir, enable_vl)` | API 投影：从最后一个 `CompactMarker` 之后开始、跳过真正空的 model turn、连续 user 用 `<multi_message/>` 合并（详见 [compact.md](./compact.md) 与下文） |
| `to_flat_dicts()` | 展开成前端 role-based 列表（init 快照 / `/history`） |

运行时序列示例（steering 在工具执行中途到达）：

```
drain 开始      append_model        → [..., model(open)]
steer 到达      append              → [..., model(open), user]
steer 再到达    append              → [..., model(open), user, user]
结果1 回来      insert_result       → [..., model, tool_results(1), user, user]   ← 插到 user 前
结果2 回来      insert_result       → [..., model, tool_results(1,2), user, user]
下一轮开始      append_model        → 旧的开放地带自动闭合
```

## TurnsView：Turn 视图与领域操作

`TurnsView` 是 `MessageList` 之上的 turn 分组视图（领域操作层），只读折叠 + 把操作拆解成 MessageList 原语，**从不直接改列表**：

```
Turn
  inputs: list[Message]   # 上一个 model 输出之后、本 model 之前的一切
                          #（前一批的 ToolResults + user/steering；顺序是扁列表的不变量）
  output: ModelMessage | None   # think/content/calls；None = 开放的半截 turn
```

折叠规则：相邻两个 `ModelMessage` 之间的一切（tool_results 块 + user 块）是后者的 inputs；`CompactMarker` 不属于任何 turn（段边界）；有 inputs 没 output 的尾巴是开放的 half-turn（revert 拆分、崩溃恢复的暂态）。对话 = `[SystemMessage] + [Turn, Turn, ...]`，段首的 system 是紧接 marker 之后那个 turn 的 inputs 第一条。

## 存储与投影的分离

发送给 API 时，`get_api_messages()` 从最后一个 `CompactMarker` **之后**开始投影（marker 自身不带内容、不投影；紧跟其后的 `SystemMessage` 成为新 system 消息），marker 之前的消息从 API 上下文丢弃但仍保留在持久化历史里供前端查看。连续的 user 消息由 `combine_new_messages` 合并成一条：单条原样透传，多条用 `<multi_message/>` 分隔，让模型知道这是多条独立用户输入（如多条 steering，或 steering + 压缩指令）。

**跳过真正空的 cancelled turn**：`get_api_messages()` 还会丢弃真正空的 `ModelMessage`（`content` 为空、无 `tool_calls`、且无 `think`）。hard-cancel/interrupt 时若 LLM stream 还在早期，run 的 CancelledError 处理会保留这条只有 think 的 partial model（前端可见，也传给 API）。两个 API 陷阱（实测 Volcengine/glm-5.2）：① content **key 缺失**会 400——`_assistant_dict` 对无 tool_calls 的 turn 始终带 content key，故不触发；② content 为**空串**虽返回 200，但模型会**静默丢弃整条 assistant 消息（连同 reasoning_content）**。因此 `to_api_dicts()` 对无 tool_calls 且 content 为空的 turn 用一个空格 `" "` 代替空串发给 API，让 reasoning 得以继承；前端投影与持久化仍保留真实空串。

## 流式与控制流

### drain 即 append：消息对象是流式输出的一等公民

每轮 turn **一开始**就 `append_model` 一个空的 `ModelMessage`（persist 语义 = 不入盘），drain 流式增量直接 mutate 这个对象（`model.think +=`、`model.content +=`、`model.tool_calls.append(...)`），并经事件流发给前端。纯文本 turn 在 drain 完后 save 定稿；带工具的 turn 在 `tool_calls` 落定后 save 一次，每个工具结果经 `insert_result` 再 save 一次。这样崩溃在 drain 中途不会留下半条记录；崩溃在工具之间则留下一个可被 `_patch_dangling_tool_calls` 补全的 model + 缺失结果的记录。

### 续跑判断：何时结束 run

`run()` 主循环每轮 turn 之前做续跑判断——只在「有新内容要模型响应」时才跑下一轮，否则结束 run：

```
has_unread_user = _count_user_messages() > _user_read_cursor
if not (_pending_tool_results or has_unread_user or _force_run):
    break   # run 结束，发 done
```

三个续跑信号：

* `_pending_tool_results` -- 上一轮产出了 `tool_calls`，模型还没看到整批结果。决定跑一轮后即清零，所以纯文本 turn 不会因此无限续跑。
* `has_unread_user` -- 自模型上次读取（`get_api_messages` 调用时刻，记录在 `_user_read_cursor`）后有新的 user 消息（start/steer）。
* `_force_run` -- 压缩后强制跑一轮，让模型在重建的上下文上继续。

`_user_read_cursor` 在 `get_api_messages()` 调用**之后**（drain 之前）更新，记录这一轮模型实际读到的 user 消息数。这样 drain 中途到达的 steer 落在 cursor 之后，驱动下一轮续跑。

## Steering（运行中追加消息）

`steer()` 直接 `append` 一个 `UserMessage` 到 history 并发 `user` 事件（事件带 `id` 和 `user_seq`）。由于 steer 只在 `is_running()` 时被调用（停机时走 `start`），run 循环正活着，下一个续跑判断会检测到这条未读消息并跑一轮让模型响应。

因为 `insert_result` 把所有结果插在**开放模型之后、任何 user 之前**（结果先到则创建条目、steer 追加在其后；steer 先到则结果插入其前），steer 物理上总是落在整批结果之后，不可能插进 `model(tool_calls)` 与其结果之间——这是扁平模型的核心收益，由 MessageList 原语保证。

发送给 API 时，连续的 user 消息（包括多条 steer）由 `get_api_messages()` 合并：

```
history:  ... ModelMessage(calls), ToolResults, UserMessage("用 Python"), UserMessage("另外加上日志")
API 收到: ... assistant, tool, tool, {user: "…<multi_message/>…"}
```

## 压缩（compaction）

压缩完全统一进主循环，没有专门的 compact 方法。流程：

1. **触发**：一轮（含工具执行）结束后读 token 用量，若超过阈值，标记 `_need_compact=True` 并把压缩指令（`build_compact_prompt`）作为一条普通 `UserMessage` append 进 history。它和任何未读的 steering 消息一样，是 user 消息——不区分「正常 prompt」和「steering prompt」。
2. **压缩 turn**：下一轮续跑判断发现有新 user 消息（压缩指令），照常跑一轮。压缩 turn 仍发送完整的工具定义（与正常轮一致，保持请求前缀稳定以命中上下文缓存），由 `COMPACTION_PROMPT` 指示模型只产出文本 handoff note、不调用工具。若模型仍调用工具，调用会被拦截（不执行，返回"正在压缩"提示），该 turn 不 finalize 而是重试直到模型输出纯文本 summary。这轮的 `get_api_messages` 会把 steering + 压缩指令用 `combine_new_messages` 合并成一条 user，模型一次性读到全部并写进 note。
3. **重建**：压缩 turn（纯文本）结束后，`_finalize_compaction` 落盘一个无内容的 `CompactMarker`、一条重建的 `SystemMessage`、以及把 note 包成 `<compact>...</compact>` 的 `compact_summary` user 消息。`get_api_messages` 从 marker 之后开始，所以压缩前的全部（steering、压缩指令、note turn）从 API 上下文丢弃，但留在持久化历史里。重置 token 计数，设 `_force_run`。
4. **继续**：`_force_run` 让主循环在重建的上下文上再跑一轮，模型基于 note 继续。

steering 不需要任何特殊处理：它在压缩 turn 被模型读到并写进 note，原文随 marker 裁剪——这正是压缩对所有上下文做的事。`COMPACTION_PROMPT` 已相应改写，告诉模型下一轮只会看到 note（不再有「最近的 user 消息会保留」的预期），需把未处理的用户请求写进 note。

手动 `/compact`（`compact_now`）走同一条路：设 `_need_compact` + append 压缩指令 + 启动 run，剩余由主循环处理。详见 [compact.md](./compact.md)。

## 保存时机

| 时机 | 说明 |
|------|------|
| `start()` / `steer()` | append `UserMessage`（立即 save） |
| drain 开始 | `append_model` 空 `ModelMessage`（不入盘） |
| 纯文本 turn drain 完 | `save()` 定稿 |
| 带 tool_calls turn drain 完 | `save()`（含 tool_calls，便于崩溃补全） |
| 每个工具执行完 | `insert_result`（创建/追加 `ToolResults` 条目并 save） |
| 取消时保留部分输出 | `save()` 落盘 in-flight 的 `ModelMessage`（有内容/think/tool_calls 时）；空壳则 `remove()` 丢弃。在飞 bash 调用由 `_salvage_inflight_tool()` 用 `_tool_output_buffer` 抢救部分输出 + 取消标注作为 result 插入 |
| 压缩 | 各步 append 后 save |

## 编辑重发（revert）

用户可以编辑之前发送的某条消息并从该处重新推理（`POST /api/sessions/{id}/revert`，参数 `message_id` + `message`，以及可选的图片载荷：`keep_images` 列出编辑后保留的旧附件文件名（未列出的即被删除）、`images` 为新上传附件）。`Agent.revert` 通过 `TurnsView.find_user` 按 `_id` 定位目标（找不到抛 ValueError → 400），先算出最终附件列表（目标消息中 `keep_images` 命中的项 + 新上传项），再按该消息是否已被模型读取分两种处理：

* **rerun**（`user_seq < _user_read_cursor`，或 agent 当前不在运行）：若正在运行先 `stop()`（必须先停后截——被取消 run 的 salvage 逻辑可能引用即将被截掉的消息），再 `history.truncate_at(message_id)` 丢弃该 user 消息及其后所有内容，把 `_user_read_cursor` 对齐到截断后的历史（否则截断后 cursor 仍指向旧的大值，`start()` 追加的新消息会被判为「已读」、run 空转直接结束），`broker.commit(len(history))` 重置回放基准，再 `start(message, images=final_images)` 携带完整附件列表重新推理。端点返回 `rerun`，前端重连 SSE。
* **queued**（steer 尚未被读取且 agent 正在运行）：直接改该 `UserMessage.content` 与 `.images`，**不中断运行**，广播 `user_edit` 事件（带 `message_id` 与最新 images）让前端就地更新。端点返回 `queued`，前端无需重连。若 agent 已不在运行，即使消息未读也走 rerun（否则没有 run 来消费这条就地改动）。

截断在扁平列表上切在目标 user 消息自己的索引处：它前面的工具结果天然保留，重跑后与编辑后的新消息一起折叠进下一个 turn 的 inputs——"拆 turn"这个操作在扁平模型里不存在。

## Fork（分叉新会话）

用户在编辑某条历史消息时可以不重发原会话，而是从这条消息分叉出一个新会话（`POST /api/sessions/{id}/fork`，参数与 `/revert` 同构：`message_id` + `message` + 可选图片载荷）。编辑框里确认按钮旁边有一个 fork 按钮：确认（勾）在原会话截断重发，fork 把同样的内容提交到一个全新会话并自动切换过去，原会话完全不动。

新会话由端点直接组装：

* **历史**：源会话 `history.raw` 中位于目标 user 消息之前的前缀，以 v3 storage dict 原样写入新会话的 `history.json`（消息 id 保留，副本独立寻址）。前缀切在 user 消息之前，天然满足顺序不变量。
* **继承**：`model`、`additional_dirs`、`project_id`、`plan_mode`、`working_dir` 从源会话的 `session.json` 复制。
* **标题**：取 fork 消息文本的前 50 字符（与自动命名同规则），并直接置 `titled=True` 定稿。
* **图片**：历史只存文件名，字节在会话的 `images/` 目录——前缀引用的图片文件与 fork 消息保留的旧附件都从源会话复制到新会话，新上传附件按普通发送存入新会话。
* **运行**：注册新 Agent 后立即 `start(message)`，fork 消息作为新会话的待推理消息启动 run（历史前缀里已有 user 消息，自动命名不会触发）。

前端 `store.fork` 调用成功后 `selectSession(新 id)`：重建会话列表并连接新会话的 SSE，in-flight run 的回放缓冲保证已发出的流事件不丢。

## 前端协议

前端拿到的是 `init` 快照里的扁平 role-based `HistoryMessage[]`（`ModelMessage` 展开成 `assistant`、`ToolResults` 展开成多条 `tool`）。字段名（`reasoning_content`、`compact_marker`、`compact_summary`、`diff`、`tool_calls`、`tool_call_id`）与 v2 一致，另外每条**带上 `id`**（user 事件和 `user_edit` 事件也带 `id`/`message_id`），编辑重发时前端把 `message_id` 传给 `/revert`。`parseHistory` 只新增了 `id` 的透传，其余无需改动。

## broker 回放

`StreamBroker` 的 `persisted_len` 语义不变：仍按 run 边界推进（仅终态 `done`/`cancelled`/`error`/`interrupted` 及压缩后 `commit` 推进），run 期间 append/mutate 的对象都在 `persisted_len` 之后，由 `_turn_buffer` 的事件流镜像覆盖。`attach` 切片 `raw[:persisted_len]`（**内存条目**计数，与磁盘条目数无关），由 `attach_subscriber` 展开成扁平 dict 放入 `init` 快照。详见 [streaming.md](./streaming.md)。

## 会话命名冲突处理

创建新 session 时，如果指定的名称已存在，后端会自动追加数字后缀避免冲突：

```
foo        -> 首次创建，使用原名
foo        -> 再次创建，自动变为 foo(2)
foo        -> 再次创建，自动变为 foo(3)
foo(2)     -> 再次创建，自动变为 foo(4)
```

前端 `createSession` 会接收并采用后端返回的实际名称，确保 UI 显示与持久化一致。