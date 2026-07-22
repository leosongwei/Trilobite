# LLM 通信层

## 概述

Agent 通过 HTTP 直接调用 OpenAI 兼容的 chat completions API（如 DeepSeek）。为避免被识别为自建 agent 而触发封号，请求伪装为 opencode 客户端。

不使用 OpenAI Python SDK（会带 `X-Stainless-*` header 暴露 Python 身份），而是通过 `httpx.AsyncClient` 直接构造请求。

## HTTP Headers

发送的请求 header：

| Header | 值 | 说明 |
|---|---|---|
| `Authorization` | `Bearer <api_key>` | 认证 |
| `Content-Type` | `application/json` | — |
| `Accept` | `text/event-stream` | 流式请求需要 |
| `User-Agent` | `opencode/1.18.4` | 伪装为 opencode |
| `x-session-affinity` | `ses_xxxxxxxxxx...` | opencode 风格 session ID |
| `X-Session-Id` | `ses_xxxxxxxxxx...` | 同上 |

不含任何 `X-Stainless-*` header。

## Session ID

格式完全匹配 opencode：`ses_` + 12 位 hex（时间戳取反） + 14 位随机 base62 = 30 字符。

示例：`ses_0754be6ddfffj0SOI5CAmWerGq`

创建 session 时生成并持久化到 `session.json` 的 `session_id` 字段，重启后复用，不会因重载 Agent 而更换。

## 流式响应解析

自定义 SSE parser（约 30 行），将 `data: {...}` 行解析为 `_StreamChunk` 数据类，字段名与 OpenAI SDK chunk 一致：

```python
chunk.choices[0].delta.content
chunk.choices[0].delta.reasoning_content
chunk.choices[0].delta.tool_calls
chunk.usage.total_tokens
```

## 非流式调用

`Agent.chat_completion(messages, stream=False)` — 用于 compaction 等场景，返回 `resp.json()` 字典。

## 相关代码

| 文件 | 内容 |
|---|---|
| `src/trilobite/agent.py` | `Agent.chat_completion()`、`_chat_completion_stream()`、`_generate_session_id()` |
| `src/trilobite/compaction.py` | 通过 `agent.chat_completion(stream=False)` 做摘要 |
