# 上下文压缩（Compaction）

## 概述

当对话历史接近模型的上下文窗口上限时，agent 会自动压缩历史：将较早的对话交给 LLM 生成摘要，用摘要替换旧消息，保留最近的消息。这样 agent 可以在长对话中持续工作而不超出上下文限制。

## 触发条件

每个 agent 循环开始时检查是否需要压缩。估算公式：

```
estimated = token_count（上次 API 返回的真实 token 数）
          + estimate(未覆盖的历史消息)
          + estimate(工具定义)
```

当 `estimated >= max_context_tokens * compaction_trigger_ratio` 时触发。

默认配置：
- `max_context_tokens`：1,048,576（1M）
- `compaction_trigger_ratio`：0.7（即 70% 时触发）

## 压缩流程

```
1. 分割历史：找到最近的分割点，将历史分为"待压缩"和"保留"两部分
2. 重新构建 system 消息（使用当前配置）
3. 发送压缩请求：system + 待压缩消息 + 压缩指令 -> LLM 生成摘要
4. 用 [system, 摘要, "Understood", ...保留消息] 替换整个历史
5. 重置 token 计数
```

### 分割点的选择

`find_compact_boundary` 从历史末尾向前找，保留最近 2~6 条消息，在安全的位置切分。安全切分点需要满足：

- 切分点的前一条不是 `user` 消息（避免拆散 user-assistant 对）
- 切分点的前一条不是带 `tool_calls` 的 `assistant` 消息（避免拆散工具调用组）
- 切分点的后一条不是 `tool` 消息（同上）

## 重新构建 system 消息

compaction 时**重新构建** system 消息，使用当前的 `~/.config/trilobite/system_prompt.txt` 和 `<working_dir>/AGENTS.md`。这与正常请求不同（正常请求从 history 中读取已存的 system 消息）。

原因：compaction 本质上是开启一段新对话，应该使用最新的项目配置。

## 压缩后的历史结构

```json
[
  { "role": "system", "content": "（重新构建的 system prompt + working_context）" },
  { "role": "user", "content": "[Context summary]\n<LLM 生成的摘要>" },
  { "role": "assistant", "content": "Understood. Continuing with the task." },
  ...最近的消息...
]
```

## 实际例子

### 压缩前

假设对话已经很长，token 估算超过阈值。此时 history 有 20 条消息：

```json
[
  { "role": "system", "content": "你是一个编码助手。\n\n<AGENTS.md>\n..." },
  { "role": "user", "content": "帮我创建一个 Web 服务器" },
  { "role": "assistant", "tool_calls": [...] },
  { "role": "tool", "content": "..." },
  { "role": "assistant", "content": "我创建了一个 FastAPI 服务器..." },
  { "role": "user", "content": "加上数据库连接" },
  { "role": "assistant", "tool_calls": [...] },
  { "role": "tool", "content": "..." },
  { "role": "assistant", "content": "已经加上了 SQLite 连接..." },
  ...更多对话...
  { "role": "user", "content": "现在帮我加个测试" },
  { "role": "assistant", "tool_calls": [...] },
  { "role": "tool", "content": "..." }
]
```

### 分割

`find_compact_boundary` 决定保留最后 4 条消息（2 个 user-assistant 对），前 16 条待压缩。

### 压缩请求

发送给 LLM 的 messages：

```json
[
  { "role": "system", "content": "（重新构建的 system prompt）" },
  { "role": "user", "content": "帮我创建一个 Web 服务器" },
  { "role": "assistant", "tool_calls": [...] },
  ...前 16 条消息...
  { "role": "user", "content": "请总结以上对话的关键信息..." }
]
```

LLM 返回摘要：

```
用户要求创建 Web 服务器，使用了 FastAPI。随后添加了 SQLite 数据库连接，
创建了 User 模型。目前项目结构包含 main.py、database.py 和 models.py。
```

### 压缩后

history 被替换为：

```json
[
  { "role": "system", "content": "（重新构建的 system prompt）" },
  { "role": "user", "content": "[Context summary]\n用户要求创建 Web 服务器，使用了 FastAPI。随后添加了 SQLite 数据库连接，创建了 User 模型。目前项目结构包含 main.py、database.py 和 models.py。" },
  { "role": "assistant", "content": "Understood. Continuing with the task." },
  ...最后 4 条消息...
]
```

### token 计数重置

压缩后 `_token_count = 0`，`_token_covered = 0`。下一次 API 请求会获得新的真实 token 数，从此开始累计。这意味着压缩后不会立即再次触发压缩。

## 前端提示

压缩进行时，后端发送 `status` SSE 事件：

```json
{ "type": "status", "text": "compacting context..." }
```

前端将其显示为状态条，让用户知道正在进行上下文压缩。

压缩重写了 history（`replace_all`），因此 run 循环在 compaction 返回后会调用 `StreamBroker.commit`：把已提交历史长度推进到当前长度并清空回放缓冲，避免重连客户端的 `init` 快照与回放缓冲重复（详见 `streaming.md`）。

## 边界情况

- **历史太短**：`find_compact_boundary` 在消息数 ≤ 3 时返回 -1，不执行压缩。
- **找不到安全分割点**：返回 -1，不执行压缩（token 会继续增长直到下次检查）。
- **压缩 API 调用失败**：摘要内容设为 `"[compaction failed - older context dropped]"`，旧消息被丢弃但保留最近消息。
