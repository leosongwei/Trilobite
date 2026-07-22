我们来造一个简单的coding agent。

# 项目架构

Trilobite 是一个基于 DeepSeek 的 coding agent，分为 **后端 (Python/FastAPI)** 和 **前端 (Vue 3/TypeScript)** 两部分。
## 后端 (`src/trilobite/`)

* `server.py` - FastAPI 应用入口。`POST /message` 启动/转向 agent（返回 JSON，agent 作为独立 task 运行，关闭浏览器不会取消运行）；`GET /stream` SSE 订阅端点广播实时输出；挂载静态前端文件。
* `agent.py` — 核心 Agent 类。管理会话生命周期：加载 working context (`AGENTS.md`)，与 LLM 流式对话，循环执行 tool calls，支持 plan/build 双模式切换和用户 steering。
* `broker.py` - 流式事件总线（StreamBroker）。解耦 agent 运行与 HTTP 请求：事件广播到所有订阅者，维护当前 run 的回放缓冲和已提交历史长度（`persisted_len`），支持浏览器多开、关闭重开、切 tab 恢复。详见 `doc/product/streaming.md`。
* `history.py` — 对话历史管理。持久化到 JSON 文件，提供 API 调用的消息合并（连续同 role 消息合并避免 API 报错）。
* `tool_call.py` — 工具注册与分发。维护工具列表，将 `exit_plan_mode` 作为内置 virtual tool 注入。
* `compaction.py` — 上下文压缩。当 token 使用超过阈值时，将旧对话压缩为摘要，拼接回历史。
* `config.py` — 配置管理。首次运行自动从 `config_example/` 复制默认配置；加载 `system_prompt.txt` 和 `compaction_prompt.txt`。
* `tokens.py` — 简易 token 估算（字符级，区分 ASCII/CJK）。
* `file_access.py` — 文件路径解析和安全检查（敏感文件过滤）。
* `tools/` — 四个具体工具：
  * `bash.py` — 执行 shell 命令
  * `read.py` — 读取文件（支持行/字符限制）
  * `write.py` — 写入文件（replace 模式生成上下文 diff）
  * `todo.py` — 任务列表管理（JSON 文件持久化在 session 目录）

## 前端 (`frontend/`)

Vue 3 + TypeScript，构建后输出到 `src/trilobite/static/`，由 FastAPI 直接 serve。

* `main.ts` — 入口
* `App.vue` — 根组件，布局 sidebar + chat + token bar
* `store.ts` - 全局状态：sessions、chat items、SSE 订阅流处理（`init` 重建对话、事件驱动 isStreaming、断线自动重连）、plan mode
* `api.ts` — HTTP API 封装
* `types.ts` — TypeScript 类型定义
* `components/` — ChatView、ChatInput、SessionSidebar、TokenBar、TurnBlock、ThinkingBlock、ToolEntry
* `utils/markdown.ts` — Markdown 渲染
* `utils/mathjax.ts` — 数学公式渲染

## Plan/Build 双模式

Agent 有两种运行模式：
* **Plan 模式**（只读）：只能使用 read、bash、TodoList 等非破坏性工具。`write` 被拦截。
* **Build 模式**（全权限）：所有工具可用。

Agent 调用 `exit_plan_mode` 时前端展示审批横幅，用户批准后切换。按 **Tab** 可在两种模式间切换。

## 文件访问权限

`read`/`write` 工具访问工作区以外的路径时，前端弹出权限请求横幅（Grant/Deny）。批准后目录加入 `additional_dirs` 并持久化到 `session.json`，重启保留。敏感文件直接拒绝。

## 上下文压缩

Token 超过 `compaction_trigger_ratio` 阈值时触发压缩。提示词采用 kimi 风格的第一人称 handoff note。TODO 列表自动附带，不转录。

## 配置 (`config_example/`)

* `config.yaml` — API 密钥、模型、token 上限、压缩触发比例
* `system_prompt.txt` — 系统提示词（含权限说明）
* `compaction_prompt.txt` — 压缩摘要提示词（kimi 风格）

# 风格和注意事项

## 工作流

* 记得更新产品文件：每次修改agent的工作方式后，需要在`doc/product`的相应文件中说明

## 后端

* 3.3后的现代python不应该用__init__.py
* 全篇采用绝对引用`from src.trilobite.xxxx import yyyy`，禁止使用相对引用

## 前端

* 不要去整个读取那些编译出来的js文件，超级大，会耗尽上下文