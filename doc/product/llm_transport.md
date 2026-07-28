# LLM 通信层

## 概述

Agent 通过 HTTP 直接调用 OpenAI 兼容的 chat completions API（如 DeepSeek）。为避免被识别为自建 agent 而触发封号，请求伪装为 opencode 客户端。

不使用 OpenAI Python SDK（会带 `X-Stainless-*` header 暴露 Python 身份），而是通过 `httpx.AsyncClient` 直接构造请求。

## HTTP Headers

发送的请求 header：

| Header | 值 | 说明 |
|---|---|---|
| `Authorization` | `Bearer <api_key>` | 认证 |
| `Content-Type` | `application/json` | -- |
| `Accept` | `text/event-stream` | 流式请求需要 |
| `User-Agent` | `opencode/1.18.4` | 伪装为 opencode |
| `x-session-affinity` | `ses_xxxxxxxxxx...` | opencode 风格 session ID |
| `X-Session-Id` | `ses_xxxxxxxxxx...` | 同上 |

不含任何 `X-Stainless-*` header。

## Session ID

格式完全匹配 opencode：`ses_` + 12 位 hex（时间戳取反） + 14 位随机 base62 = 30 字符。

示例：`ses_0754be6ddfffj0SOI5CAmWerGq`

创建 session 时生成并持久化到 `session.json` 的 `session_id` 字段，重启后复用，不会因重载 Agent 而更换。

## 请求体

`chat_completion()` 构造的 body 关键字段：

| 字段 | 来源 | 说明 |
|---|---|---|
| `model` | config | 模型名 |
| `messages` | history | 对话消息 |
| `tools` | tool_call | 工具定义（可选） |
| `stream` | 参数 | 是否流式 |
| `stream_options` | 固定 | `{include_usage: true}`，流式时附带 token 用量 |
| `reasoning_effort` | config | 思考强度，流式时发送 |
| `thinking` | 固定 | `{type: enabled}`，流式时启用思考 |
| `max_tokens` | config | **单次 completion 的输出 token 上限** |

`max_tokens`（单次输出上限，默认 65536）与 `max_context_tokens`（上下文窗口，默认 1048576）是两个不同维度：前者限制单轮回复能生成多少 token，后者限制整段历史能占多大窗口。

> 历史教训：若不传 `max_tokens`，API 默认 4096。配合 `reasoning_effort: max` 时，模型可能在 reasoning 阶段耗尽 4096 token，导致 `finish_reason=length`、正文 `content` 一字未生即被截断，表现为"卡住/空回复"。务必显式设置足够大的 `max_tokens`。

## 流式响应解析

自定义 SSE parser（约 30 行），将 `data: {...}` 行解析为 `_StreamChunk` 数据类，字段名与 OpenAI SDK chunk 一致：

```python
chunk.choices[0].delta.content
chunk.choices[0].delta.reasoning_content
chunk.choices[0].delta.tool_calls
chunk.usage.total_tokens
```

> 历史教训：不同 provider 对「没有 tool_calls 的 delta」的表示不一致。DeepSeek 直接省略 `tool_calls` 键，而 GLM（经 opencode zen）会在每个 delta chunk 显式写 `"tool_calls": null`。`dict.get("tool_calls", [])` 仅在键缺失时返回默认值，键存在但值为 `null` 时返回 `None`，迭代 `None` 会抛 `TypeError: 'NoneType' object is not iterable`，表现为切到该 provider 后首轮就崩。解析时统一用 `d.get("tool_calls") or []`，同时覆盖缺失与 null 两种情况。

## 非流式调用

`Agent.chat_completion(messages, stream=False)` -- 用于 compaction 等场景，返回 `resp.json()` 字典。

## 调试日志

每个 session 的 LLM 通信细节记录到 `sessions/<name>/agent.log`，用于排查流式输出被截断等问题。

日志由 `Agent.__init__` 创建的 `logging.FileHandler` 写入，logger 名 `trilobite.agent.<name>`，不向上传播。日志级别由 config 的 `log_level` 控制，**默认 `WARNING`**（只记录异常：`STREAM ended WITHOUT [DONE]`、`STREAM error`、`RUN cancelled/error`、`TURN produced EMPTY`）。需要排查时改为 `DEBUG` 可记录每条 SSE 原始行与每个 chunk 的解析结果。

记录内容：

| 事件 | 级别 | 说明 |
|---|---|---|
| `STREAM request` | INFO | 请求 model / 消息数 / 工具数 / reasoning |
| `STREAM response` | INFO | 响应 status / content-type |
| `STREAM raw>` | DEBUG | 每条 SSE 原始行（截断 800 字符） |
| `STREAM chunk#N` | DEBUG | 每个 chunk 的 content/reasoning/tool_calls 长度与 `finish_reason` |
| `STREAM [DONE]` | INFO | 正常结束，附带 finish_reasons 列表 |
| `STREAM ended WITHOUT [DONE]` | WARNING | 流提前关闭、未收到 `[DONE]` |
| `STREAM error` | ERROR | 网络/HTTP 异常 |
| `TURN result` | INFO | 每轮累积的 content/thinking/tool_calls 长度、token 数、plan 模式 |
| `TURN produced EMPTY assistant output` | WARNING | content/thinking/tool_calls 全为空（即复现的截断现象） |
| `RUN cancelled` / `RUN error` | WARNING/ERROR | 取消或异常时记录残余输出 |

排查「只输出思考、没有正文」时，重点看 `finish_reason`（`length` 说明被 token 上限截断；`stop` 说明模型主动结束）、`STREAM ended WITHOUT [DONE]`（连接异常关闭）以及 `TURN produced EMPTY`。

## 相关代码

| 文件 | 内容 |
|---|---|
| `src/trilobite/agent.py` | `Agent.chat_completion()`、`_chat_completion_stream()`、`_generate_session_id()` |
| `src/trilobite/compaction.py` | 通过 `agent.chat_completion(stream=False)` 做摘要 |
