# 上下文构建

## 概述

每次 API 请求发送的消息序列由两部分组成：**system 消息** 和 **对话历史**。system 消息在首次使用时被构建并写入 history，之后不再重新构建——这样即使项目配置发生变化，已有的对话上下文也不会"超时空"地被篡改。

## system 消息的组成

system 消息的内容是两段拼接：

```
{system_prompt}

<AGENTS.md>
{working_dir}/AGENTS.md 的内容
</AGENTS.md>
```

| 部分 | 来源 | 说明 |
|------|------|------|
| `system_prompt` | `config/system_prompt.txt` | agent 的基础指令 |
| `working_context` | `<working_dir>/AGENTS.md` | 如果工作目录下存在 `AGENTS.md`，将其内容用 `<AGENTS.md>` 标签包裹后追加；不存在则为空 |

## 生命周期

### 首次使用（新 session 或无 system 消息的旧 history）

`_ensure_system_message()` 检查 history 的第一条消息是否为 system 角色。如果不是，用当前配置构建并插入到 history 头部：

```python
history.insert(0, {"role": "system", "content": system_prompt + working_context})
```

### 正常请求

system 消息已经在 history 中，直接将整个 history 作为 messages 发送：

```python
messages = self.history  # 第一条就是 system 消息
```

**不重新构建** system 消息。即使此时 `config/system_prompt.txt` 或 `AGENTS.md` 发生了变化，history 中的 system 消息保持不变。

### Compaction 时

compaction 会**重新构建** system 消息（使用当前配置），因为 compaction 本质上是开启一段新的对话。详见 [compact.md](./compact.md)。

## 实际例子

假设 `config/system_prompt.txt` 内容为：

```
你是一个编码助手。
```

工作目录 `/home/user/myproject` 下有 `AGENTS.md`：

```
# 项目规范
使用 Python 3.12
```

### 首次请求

用户输入"帮我写个脚本"，发送给 API 的 messages：

```json
[
  {
    "role": "system",
    "content": "你是一个编码助手。\n\n<AGENTS.md>\n# 项目规范\n使用 Python 3.12\n</AGENTS.md>"
  },
  {
    "role": "user",
    "content": "帮我写个脚本"
  }
]
```

同时 history.json 被写入：

```json
[
  {
    "role": "system",
    "content": "你是一个编码助手。\n\n<AGENTS.md>\n# 项目规范\n使用 Python 3.12\n</AGENTS.md>"
  },
  {
    "role": "user",
    "content": "帮我写个脚本"
  }
]
```

### 后续请求

此时即使修改了 `config/system_prompt.txt` 或 `AGENTS.md`，history 中的 system 消息不会改变。下一次 API 请求的 messages 直接来自 history：

```json
[
  { "role": "system", "content": "你是一个编码助手。\n\n<AGENTS.md>\n..." },
  { "role": "user", "content": "帮我写个脚本" },
  { "role": "assistant", "content": "好的，我来帮你..." },
  { "role": "user", "content": "改成 Rust" }
]
```

### 重启后

服务重启后，session 从 history.json 恢复。`_ensure_system_message()` 发现 history 第一条已经是 system 消息，不做任何修改。system 消息保持上次会话时的内容。
