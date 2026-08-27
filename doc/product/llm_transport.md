# LLM 通信层

## 概述

Agent 通过 HTTP 直接调用 OpenAI 兼容的 chat completions API（如 DeepSeek）。为避免被识别为自建 agent 而触发封号，请求伪装为 opencode 客户端。

不使用 OpenAI Python SDK（会带 `X-Stainless-*` header 暴露 Python 身份），而是通过 `httpx.AsyncClient` 直接构造请求。

## HTTP Headers

发送的请求 header 由模型定义的 `pretend_to_be_opencode` 字段控制。默认 `true`：

| Header | 值 | 说明 |
|---|---|---|
| `Authorization` | `Bearer <api_key>` | 认证 |
| `Content-Type` | `application/json` | -- |
| `Accept` | `text/event-stream` | 流式请求需要 |
| `User-Agent` | `opencode/1.18.4` | 伪装为 opencode |
| `x-session-affinity` | `ses_xxxxxxxxxx...` | opencode 风格 session ID |
| `X-Session-Id` | `ses_xxxxxxxxxx...` | 同上 |

不含任何 `X-Stainless-*` header。

当 `pretend_to_be_opencode: false` 时，请求头退化为最小标准集：

| Header | 值 | 说明 |
|---|---|---|
| `Authorization` | `Bearer <api_key>` | 仅当 `api_key` 非空时发送 |
| `Content-Type` | `application/json` | -- |
| `Accept` | `text/event-stream` | 流式请求需要 |

此设置用于本地 llama.cpp 等不需要 Bearer 认证或不需要 opencode 伪装头的服务端。将对应模型的 `api_key` 留空（空字符串），同时设置 `pretend_to_be_opencode: false`，即可完全省略 `Authorization` 头，请求头中只保留 `Content-Type` 和（流式时的）`Accept`。

## Session ID

格式完全匹配 opencode：`ses_` + 12 位 hex（时间戳取反） + 14 位随机 base62 = 30 字符。

示例：`ses_0754be6ddfffj0SOI5CAmWerGq`

创建 session 时生成并持久化到 `session.json` 的 `session_id` 字段，重启后复用，不会因重载 Agent 而更换。

## 请求体

`chat_completion()` 构造的 body 关键字段：

| 字段 | 来源 | 说明 |
|---|---|---|
| `model` | 会话当前模型的 `model` | 模型名（会话切换模型后下一次请求生效） |
| `messages` | history | 对话消息 |
| `tools` | tool_call | 工具定义（可选） |
| `stream` | 参数 | 是否流式 |
| `stream_options` | 固定 | `{include_usage: true}`，流式时附带 token 用量 |
| `max_tokens` | 会话当前模型的 `max_tokens` | **单次 completion 的输出 token 上限** |
| `reasoning_effort` / `thinking` 等 | 会话当前模型的 `extra_body` | 模型自定义字段，**原样合并**进请求体（如思考模式 `{"reasoning_effort": "max"}`） |

`max_tokens`（单次输出上限，默认 65536）与 `max_context_tokens`（上下文窗口，默认 1048576）是两个不同维度：前者限制单轮回复能生成多少 token，后者限制整段历史能占多大窗口。两者都属于模型定义的一部分，随会话切换模型而更新。

> 历史教训：若不传 `max_tokens`，API 默认 4096。配合 `reasoning_effort: max` 时，模型可能在 reasoning 阶段耗尽 4096 token，导致 `finish_reason=length`、正文 `content` 一字未生即被截断，表现为"卡住/空回复"。务必显式设置足够大的 `max_tokens`。思考模式字段由用户在各模型的 `extra_body` 中显式声明，agent 不再内置注入。

## 流式响应解析

自定义 SSE parser（约 30 行），将 `data: {...}` 行解析为 `_StreamChunk` 数据类，字段名与 OpenAI SDK chunk 一致：

```python
chunk.choices[0].delta.content
chunk.choices[0].delta.reasoning_content
chunk.choices[0].delta.tool_calls
chunk.usage.total_tokens
```

> 历史教训：不同 provider 对「没有 tool_calls 的 delta」的表示不一致。DeepSeek 直接省略 `tool_calls` 键，而 GLM（经 opencode zen）会在每个 delta chunk 显式写 `"tool_calls": null`。`dict.get("tool_calls", [])` 仅在键缺失时返回默认值，键存在但值为 `null` 时返回 `None`，迭代 `None` 会抛 `TypeError: 'NoneType' object is not iterable`，表现为切到该 provider 后首轮就崩。解析时统一用 `d.get("tool_calls") or []`，同时覆盖缺失与 null 两种情况。

## 流式回合重试

低质量服务商存在两类典型故障：**请求级失败**（任意 HTTP 错误，如 503/429/4xx）和 **流中途断开**（未收到 `[DONE]` 标记、也没有 finish reason 就关闭连接——思维链刚生成一半或工具调用片段刚发出即断）。二者统一为同一个重试机制：

* **统一异常** `_StreamAttemptError`：`_chat_completion_stream` 把一切失败归一为该异常（HTTP 错误带状态码、传输错误带简短标签、流未正常结束抛「连接中断」），回合层不再关心具体失败种类。
* **丢弃部分输出**：失败回合的 `model` 消息从未落盘（`append_model(persist=False)`），重试前清空其思维链/正文/工具调用片段并从 history 移除，**重发完全相同的请求**（复用回合开始时快照的 `messages`，保证重放一致、前缀缓存命中）。
* **完成条件校验**：工具调用回合必须输出非思维链的 `content` 才算完整——「调用工具后连接关闭」的回合（有 tool_calls、无正文）按失败处理重试；compaction 回合（工具调用被拦截、依赖结果连续重试）豁免该校验。
* **可见的重试流程**：每次重试前广播 `status` 事件（`⚠️ LLM request failed (<原因>), retrying (k/N)...`，前端顶部横幅显示）和 `turn_restart` 事件（前端丢弃本回合已流出的部分输出、开启新泡泡），随后线性退避（1s、2s、…上限 5s）。
* **尝试上限**：`max_stream_retries` 配置（默认 3，含首次请求），达到上限后丢弃部分输出并把最后一次异常交给 run 的错误路径（`error` 事件，带 status_code/body 供前端展示服务商错误信息）。

中断总结回合（`_summarize_and_exit`）复用同一机制，subagent 总结不会因一次 503 直接失败。

## 非流式调用

`Agent.chat_completion(messages, stream=False)` -- 用于 compaction 等场景，返回 `resp.json()` 字典。

## VLM / 图片输入

当会话当前模型的 `enable_vl: true` 时，前端在 send 按钮左侧显示“添加图片”按钮，允许一次附带多张图片。视觉能力是模型定义的一部分，切换主模型会同步切换该开关（详见 `models.md`）。

图片上传流程：

1. 前端把图片转成 base64 `data_url`，随 `/api/sessions/{id}/message` 一起发送。
2. 后端把二进制内容写入 `sessions/<id>/images/<hash>.ext`，并在 `UserMessage` 里保留 `Image(filename, mime_type, original_name)` 元数据。
3. `History.get_api_messages(image_dir=...)` 在构建 API 请求时读取图片文件、base64 编码，生成 OpenAI 兼容的 `content` 数组：

```json
{
  "role": "user",
  "content": [
    {"type": "text", "text": "看看这张图"},
    {"type": "image_url", "image_url": {"url": "data:image/png;base64,...", "detail": "auto"}}
  ]
}
```

图片文件通过 `/api/sessions/{id}/images/{filename}` 提供给前端渲染。

如果之后把 `enable_vl` 改回 `false`，新上传的图片会被丢弃，但历史中的图片元数据和文件会保留；`History.get_api_messages(enable_vl=false)` 会在构造 LLM 请求时自动去掉图片 part，只保留文字，让同一份历史可以在非视觉模型上继续。

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
