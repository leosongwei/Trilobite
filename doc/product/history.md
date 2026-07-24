# 对话历史

## 概述

对话历史是 agent 的记忆载体，由 `History` 类管理，持久化在 `~/.config/trilobite/sessions/<session_name>/history.json`。每次用户请求和 API 返回都会全量序列化到这个文件。history 的第一条消息始终是 system 消息（详见 [context_building.md](./context_building.md)）。

## History 类

`History` 类封装了历史的读取、保存和 API 投影功能：

| 方法 | 说明 |
|------|------|
| `append(msg)` | 追加一条消息并保存 |
| `extend(msgs)` | 批量追加并保存 |
| `insert(i, msg)` | 在指定位置插入并保存 |
| `replace_all(msgs)` | 替换全部消息并保存 |
| `truncate(index)` | 丢弃从 `index` 开始的所有消息并保存（revert 用） |
| `get_api_messages()` | 返回合并后的消息列表（用于发送 API） |
| `raw` | 原始消息列表（用于 compaction、history endpoint 等） |

### 存储与投影的分离

消息在 history.json 中**逐条独立存储**，保留原始边界。发送给 API 时，`get_api_messages()` 会将连续的 user 消息合并为一条（文本用 `\n\n` 连接），避免连续同角色消息导致 API 异常。

```
history.json 存储:                    API 收到:
{"role": "user", "content": "用 Python"}     ┐
{"role": "user", "content": "另外加上日志"}  ┘ -> {"role": "user", "content": "用 Python\n\n另外加上日志"}
```

## 消息类型

history 中包含四种角色的消息：

### user（用户输入）

```json
{ "role": "user", "content": "帮我读一下 main.py" }
```

### assistant - 带工具调用

```json
{
  "role": "assistant",
  "tool_calls": [
    {
      "id": "call_abc123",
      "type": "function",
      "function": {
        "name": "read",
        "arguments": "{\"filename\":\"main.py\"}"
      }
    }
  ],
  "content": "让我先读一下这个文件。",
  "reasoning_content": "用户想看 main.py 的内容，我需要调用 read 工具。"
}
```

- `tool_calls`：工具调用数组，`function.arguments` 是 JSON 字符串（非对象）
- `content`：仅在有文本输出时存在
- `reasoning_content`：仅在有思维链时存在

### assistant - 最终回复

```json
{
  "role": "assistant",
  "content": "这个文件是一个 FastAPI 应用，主要包含...",
  "reasoning_content": "我已经读取了文件内容，现在需要给用户一个清晰的总结。"
}
```

### tool（工具执行结果）

```json
{
  "role": "tool",
  "tool_call_id": "call_abc123",
  "content": "1: from fastapi import FastAPI\n2: \n3: app = FastAPI()\n..."
}
```

`tool_call_id` 对应前一个 assistant 消息中 `tool_calls[].id`。

> edit 工具的结果消息还会附带一个 `diff` 字段（结构化行级 diff：`[{type, old, new, text}]`，带真实文件行号），仅供前端回放渲染、不发给 LLM。早期会话持久化的是 `diff_prev`/`diff_current` 文本片段，前端回放时作为降级 fallback。

## 保存时机

以下操作后都会自动序列化 history 到 `history.json`（`append`/`extend`/`insert`/`replace_all` 均自动保存）：

| 时机 | 代码位置 | 说明 |
|------|----------|------|
| 添加用户消息 | `add_user_message()` | 用户发送消息时 |
| 保存 assistant + tool 消息 | `run()` 工具调用分支 | 每条消息追加时 |
| 保存 assistant 最终回复 | `run()` done 分支 | 模型给出最终答案时 |
| Steering 消息注入 | `_check_steer()` | 用户在运行中追加消息时 |
| 取消时保存部分输出 | `run()` CancelledError | 保留已有的思维链和文本 |
| Compaction 后 | `compact_if_needed()` | 用压缩后的历史替换 |
| System 消息初始化 | `_ensure_system_message()` | 首次使用时插入 system 消息 |

## 实际例子

### 一个完整的工具调用对话

用户输入"帮我读一下 main.py"，模型调用 read 工具后给出总结。history.json：

```json
[
  {
    "role": "system",
    "content": "你是一个编码助手。\n\n<AGENTS.md>\n..."
  },
  {
    "role": "user",
    "content": "帮我读一下 main.py"
  },
  {
    "role": "assistant",
    "tool_calls": [
      {
        "id": "call_abc123",
        "type": "function",
        "function": {
          "name": "read",
          "arguments": "{\"filename\":\"main.py\"}"
        }
      }
    ],
    "reasoning_content": "用户想看 main.py，调用 read 工具。"
  },
  {
    "role": "tool",
    "tool_call_id": "call_abc123",
    "content": "1: from fastapi import FastAPI\n2: app = FastAPI()"
  },
  {
    "role": "assistant",
    "content": "main.py 是一个 FastAPI 应用，只有两行代码。"
  }
]
```

### 模型只返回思维链没有文本输出

这种情况也会忠实记录（`content` 为空字符串）：

```json
[
  { "role": "user", "content": "你好呀" },
  {
    "role": "assistant",
    "content": "",
    "reasoning_content": "对方发了中文的'你好呀'，看起来就是打个招呼..."
  },
  { "role": "user", "content": "hmmm" },
  {
    "role": "assistant",
    "content": "你好呀！有什么可以帮你的吗？"
  }
]
```

### Steering（运行中追加消息）

用户发送"写个脚本"，模型开始调用工具。在工具执行间隙，用户追加"用 Python"：

history.json 中**独立存储**每条 steering 消息：

```json
[
  { "role": "system", "content": "..." },
  { "role": "user", "content": "写个脚本" },
  {
    "role": "assistant",
    "tool_calls": [{ "id": "call_1", "type": "function", "function": { "name": "bash", "arguments": "{}" } }]
  },
  { "role": "tool", "tool_call_id": "call_1", "content": "..." },
  { "role": "user", "content": "用 Python" },
  {
    "role": "assistant",
    "content": "好的，我用 Python 来写这个脚本。"
  }
]
```

### 多条 steering 消息

用户在同一个工具执行间隙连发多条消息时，每条消息**独立存储**在 history 中：

```json
[
  { "role": "tool", "tool_call_id": "call_1", "content": "..." },
  { "role": "user", "content": "用 Python" },
  { "role": "user", "content": "另外加上日志" }
]
```

发送给 API 时，`get_api_messages()` 自动合并连续的 user 消息：

```json
[
  { "role": "tool", "tool_call_id": "call_1", "content": "..." },
  { "role": "user", "content": "用 Python\n\n另外加上日志" }
]
```

这样既保留了原始消息边界（history.json 忠实记录），又避免了 API 对连续同角色消息的兼容性问题。

## 会话命名冲突处理

创建新 session 时，如果指定的名称已存在，后端会自动追加数字后缀避免冲突：

```
foo        → 首次创建，使用原名
foo        → 再次创建，自动变为 foo(2)
foo        → 再次创建，自动变为 foo(3)
foo(2)     → 再次创建，自动变为 foo(4)
```

前端 `createSession` 会接收并采用后端返回的实际名称，确保 UI 显示与持久化一致。

## 编辑重发（revert）

用户可以编辑之前发送的某条消息并从该处重新推理（`POST /api/sessions/{id}/revert`，参数 `user_seq` + `message`）。`Agent.revert` 按该消息是否已被模型读取分两种处理：

* **已在 history 中**（模型已读）：若正在运行先 `stop()`（cancel 并等待 run 结束），用 `history.truncate(target)` 丢弃该 user 消息及其后所有内容，`broker.commit(target)` 重置回放基准，再 `start(message)` 重新推理。端点返回 `rerun`，前端重连 SSE，由 `init`（截断后的历史）+ 回放缓冲（新 user 事件 + 新 run）重建对话。
* **仍在 steer 队列中**（模型尚未读取）：直接替换队列中对应消息，**不中断运行**，并广播 `user_edit` 事件让前端就地更新气泡文本。端点返回 `queued`，前端无需重连。

`user_seq` 由后端在 `user` 事件中携带（`start`/`steer` 时计算），保证前后端对用户消息序号的理解一致。
