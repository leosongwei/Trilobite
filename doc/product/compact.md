# 上下文压缩（Compaction）

## 概述

当对话历史接近模型的上下文窗口上限时，agent 会自动压缩上下文。压缩不会删除任何历史消息——**前端历史与后端 API 历史分离**：所有消息始终保留在 `history.json` 中供前端完整展示，而发给 LLM 的消息从最后一个 compact marker 开始截取。这样既不丢失对话记录，又能控制 API 上下文大小。

## Compact marker

压缩时在历史中插入一个 marker 消息：

```json
{"role": "system", "content": "<重建的系统提示词>", "compact_marker": true}
```

`get_api_messages()` 找到最后一个 `compact_marker`，从它开始取消息发给 LLM。marker 之前的所有消息不再进入 API 请求，但仍留在 `history.json` 中。前端把 marker 渲染为一道分隔线（"context compacted"），不显示系统提示词内容。

## 触发条件

每个 agent 循环开始时检查是否需要压缩。估算公式：

```
estimated = token_count（上次 API 返回的真实 token 数，仅反映 marker 之后的消息）
          + estimate(未覆盖的历史消息)
          + estimate(工具定义)
```

当 `estimated >= max_context_tokens * compaction_trigger_ratio` 时触发。

默认配置：
- `max_context_tokens`：1,048,576（1M）
- `compaction_trigger_ratio`：0.7（即 70% 时触发）

## 手动触发：/compact 命令

用户可以在输入框发送 `/compact` 手动触发压缩，无需等到 token 超阈值。

- 前端输入 `/` 时弹出命令补全菜单（纯客户端提示，实际发送的仍是文本，由后端匹配）。
- 后端 `POST /message` 识别 `message.strip() == "/compact"`：若 agent 正在运行则返回 409，否则调用 `agent.compact_now()`。
- `compact_now()` 强制压缩（跳过阈值检查），走与自动压缩相同的流程。压缩期间若有 steering 消息到达，则压缩结束后继续 run 消费它们。若没有可压缩的对话内容（如刚压缩完又立即触发），发送 status 提示而非实际压缩。

## 压缩流程

压缩不再单独调用 completion API，而是作为一次正常对话回合执行：

```
1. 追加 compact 指令（`COMPACTION_PROMPT` + todo list）作为 user 消息
2. 流式调用 LLM（tools=None），模型输出 handoff 摘要——前端实时可见
3. 追加 assistant 消息（摘要原文）
4. 重建系统提示词（`SYSTEM_PROMPT` + AGENTS.md），追加为 compact marker
5. 追加 compact summary：{role: user, content: "<compact>摘要</compact>", compact_summary: true}
6. 发送 `compact` SSE 事件，前端据此在 live view 插入分隔线（与刷新后 parseHistory 重建一致）
7. 重置 token 计数，commit broker
```

压缩后 history 尾部结构：

```
..., {user: compact指令}, {assistant: 摘要},
{system: 重建提示词, compact_marker}, {user: <compact>摘要</compact>, compact_summary},
...新对话...
```

### compact summary 与前端显示

模型输出的摘要作为 assistant 消息（步骤 3）保留在前端历史中，是压缩过程的可见记录。marker 之后的 `<compact>` 摘要（步骤 5）则是后续 API 调用实际携带的上下文载体，内容与前者相同，因此前端不渲染它（`parseHistory` 跳过 `compact_summary`），避免同一份摘要显示两次。这样 live view 和刷新后都只显示一次摘要 + 一道分隔线。

### live view 的分隔线

压缩完成后后端发送 `compact` SSE 事件，前端收到后关闭当前 turn 并插入分隔线，因此 live view 在流式摘要末尾就能看到分隔线，无需刷新。刷新时 `parseHistory` 从完整历史重建，marker 同样渲染为分隔线，两者一致。

### compact summary 为 user 角色

marker（system）后面如果直接跟 assistant 消息会让模型困惑（没有对应的 user 输入）。因此 compact summary 以 `role: user` 存储，但前端通过 `compact_summary` 标记渲染为 assistant 风格的 turn。`<compact>` 标签帮助模型区分这是上下文摘要而非真实用户输入。

### user_seq 与 compact summary

compact summary 虽然是 `role: user`，但不是真实的用户消息，不参与 `user_seq` 编号（`_count_user_messages` 排除它），保证 revert/edit 功能的序号正确。

## 重新构建系统提示词

compaction 时**重新构建**系统提示词，使用当前的 `SYSTEM_PROMPT`（代码常量）和 `<working_dir>/AGENTS.md`。这与正常请求不同（正常请求从 history 中读取已存的 system 消息）。

原因：compaction 本质上是开启一段新对话，应该使用最新的项目配置。
