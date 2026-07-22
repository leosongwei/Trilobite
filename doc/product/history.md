# 对话历史

## 概述

对话历史是 agent 的记忆载体，持久化在 `config/sessions/<session_name>/history.json`。每次用户请求和 API 返回都会全量序列化到这个文件。history 的第一条消息始终是 system 消息（详见 [context_building.md](./context_building.md)）。

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

## 保存时机

以下操作后都会全量序列化 history 到 `history.json`：

| 时机 | 代码位置 | 说明 |
|------|----------|------|
| 添加用户消息 | `add_user_message()` | 用户发送消息时 |
| 保存 assistant + tool 消息 | `run()` 工具调用分支 | 每轮工具调用结束后 |
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

steering 消息在工具调用结束后被注入到 history 中，下一轮 API 请求会看到这条新输入。

### 多条 steering 消息

用户在同一个工具执行间隙连发多条消息时，所有消息都会被保留，合并为一条 user 消息（用空行分隔），避免出现连续的 user 消息导致 API 异常：

```
用户连发: "用 Python" + "另外加上日志"
->
{"role": "user", "content": "用 Python\n\n另外加上日志"}
```
