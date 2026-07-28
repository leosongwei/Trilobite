# 上下文压缩（Compaction）

## 概述

当对话历史接近模型的上下文窗口上限时，agent 会自动压缩上下文。压缩不会删除任何历史消息--**前端历史与后端 API 历史分离**：所有消息始终保留在 `history.json` 中供前端完整展示，而发给 LLM 的消息从最后一个 compact marker 之后开始截取。这样既不丢失对话记录，又能控制 API 上下文大小。

## Compact marker

压缩时在历史中插入一个**无内容的** `CompactMarker`（纯裁剪标记），紧跟一条重建的 `SystemMessage`：

```json
{ "type": "compact_marker" },
{ "type": "system", "content": "<重建的系统提示词>" }
```

`get_api_messages()` 找到最后一个 `CompactMarker`，从它**之后**开始取消息发给 LLM（marker 自身不投影）。marker 之前的所有消息不再进入 API 请求，但仍留在 `history.json` 中。前端把 marker 渲染为一道分隔线（"context compacted"），不显示系统提示词内容。

## 触发条件

压缩不再是循环顶部的一次性检查，而是融入主循环：一轮（含工具执行）结束后读取该轮 API 返回的真实 token 用量，估算下一轮的消耗：

```
estimated = token_count（上次 API 返回的真实 token 数，仅反映 marker 之后的消息）
          + estimate(未覆盖的历史消息)
          + estimate(工具定义)
```

当 `estimated >= max_context_tokens * compaction_trigger_ratio` 时，标记 `_need_compact=True` 并把压缩指令作为一条普通 user 消息 append 进 history。下一轮因此有「新消息」而续跑。

默认配置：
- `max_context_tokens`：1,048,576（1M）
- `compaction_trigger_ratio`：0.7（即 70% 时触发）

## 压缩流程

压缩完全统一进主循环，没有专门的 compact 方法：

1. **触发**：一轮结束后 token 超阈值 -> `_need_compact=True` + append 压缩指令（`COMPACTION_PROMPT` + todo list）作为普通 `UserMessage`。它和未读的 steering 消息一样是 user 消息，由 `combine_new_messages` 合并。
2. **压缩 turn**：下一轮续跑判断发现有新 user 消息（压缩指令），照常跑一轮；但 `_need_compact=True` 时 `tools=None`（关闭工具），模型只能产出文本 handoff note。模型在这一轮的 `get_api_messages` 里一次性读到 steering + 压缩指令（合并成一条 user），把未处理的用户请求写进 note。
3. **重建**：压缩 turn（纯文本）结束后，`_finalize_compaction` 落盘：
   - 无内容的 `CompactMarker`
   - 重建的 `SystemMessage`（`SYSTEM_PROMPT` + AGENTS.md）
   - 把 note 包成 `<compact>...</compact>` 的 `compact_summary` user 消息

   ```
   ..., {user: steering...}, {user: 压缩指令}, {assistant: note},
   {CompactMarker}, {system: 重建提示词}, {user: <compact>note</compact>, compact_summary},
   ...新对话...
   ```

   `get_api_messages` 从 marker 之后开始，所以压缩前的全部（steering、压缩指令、note turn）从 API 上下文丢弃，但留在持久化历史里。重置 token 计数为 0，设 `_force_run`。
4. **继续**：`_force_run` 让主循环在重建的上下文上再跑一轮，模型基于 note 继续。

steering 不需要任何特殊处理：它在压缩 turn 被模型读到并写进 note，原文随 marker 裁剪。`COMPACTION_PROMPT` 已改写，告诉模型下一轮只会看到 note（不再有「最近的 user 消息会保留」的预期）。

## 手动触发：/compact 命令

用户可以在输入框发送 `/compact` 手动触发压缩，无需等到 token 超阈值。

- 前端输入 `/` 时弹出命令补全菜单（纯客户端提示，实际发送的仍是文本，由后端匹配）。
- 后端 `POST /message` 识别 `message.strip() == "/compact"`：若 agent 正在运行则返回 409，否则调用 `agent.compact_now()`。
- `compact_now()` 走与自动压缩同一条路：设 `_need_compact=True` + append 压缩指令 + 启动 run，剩余由主循环处理。若没有可压缩的对话内容（如刚压缩完又立即触发），发送 status 提示而非实际压缩。

## compact summary 与前端显示

模型输出的 note 作为 assistant 消息保留在前端历史中，是压缩过程的可见记录。marker 之后的 `<compact>` note 则是后续 API 调用实际携带的上下文载体，内容与前者相同，因此前端不渲染它（`parseHistory` 跳过 `compact_summary`），避免同一份摘要显示两次。这样 live view 和刷新后都只显示一次摘要 + 一道分隔线。

### live view 的分隔线

压缩重建完成后后端发送 `compact` SSE 事件，前端收到后关闭当前 turn 并插入分隔线，因此 live view 在流式摘要末尾就能看到分隔线，无需刷新。刷新时 `parseHistory` 从完整历史重建，marker 同样渲染为分隔线，两者一致。

### compact summary 为 user 角色

marker 之后如果直接跟 assistant 消息会让模型困惑（没有对应的 user 输入）。因此 compact summary 以 `role: user` 存储，但前端通过 `compact_summary` 标记渲染为 assistant 风格的 turn。`<compact>` 标签帮助模型区分这是上下文摘要而非真实用户输入。

### user_seq 与 compact summary

compact summary 虽然是 `role: user`，但不是真实的用户消息，不参与 `user_seq` 编号（`_count_user_messages` 排除它），保证 revert/edit 功能的序号正确。

## 重新构建系统提示词

compaction 时**重新构建**系统提示词，使用当前的 `SYSTEM_PROMPT`（代码常量）和 `<working_dir>/AGENTS.md`。这与正常请求不同（正常请求从 history 中读取已存的 system 消息）。

原因：compaction 本质上是开启一段新对话，应该使用最新的项目配置。
