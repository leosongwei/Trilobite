我们来造一个简单的coding agent。

# 项目架构

Trilobite 是一个 coding agent，通过 OpenAI 兼容 API 调用 LLM（默认 DeepSeek），分为 **后端 (Python/FastAPI)** 和 **前端 (Vue 3/TypeScript)** 两部分。
## 后端 (`src/trilobite/`)

* `server.py` - FastAPI 应用入口。会话级 REST API：`POST /api/sessions/{name}/message` 启动/转向 agent（返回 JSON，agent 作为独立 asyncio task 运行，关闭浏览器不取消）、`GET /api/sessions/{name}/stream` SSE 订阅实时输出；另有会话 CRUD、mode 切换、permission/plan_exit 审批、`/revert`、`/compact`、`/interrupt`、additional_dirs 增删、`/history`、`/info`、图片 serve、文件管理器 `fs/list`/`fs/file`/`fs/diff`/`fs/file`(PUT) 等端点；挂载静态前端文件。token 访问控制：启动时生成随机 key（写入配置目录 `token` 文件并打印带 `?token=` 的链接），`/api/auth/status`、`/api/auth/login` 换取 HttpOnly cookie，其余 `/api/*` 由中间件校验。`main()` 用 argparse 分发：`-t`/`-c` 进入 CLI 模式，默认 `-s` 启动 uvicorn (0.0.0.0:2345)，关闭 access log。详见 `doc/product/security.md`、`doc/product/file_manager.md`。
* `cli.py` -- 命令行交互模式（`trilobite -t [dir]` 新建 / `-c` 续接当前目录最新主 session / `-s` 服务器为默认）。纯订阅者 + 渲染器：实例化 `Agent`、`attach_subscriber()` 订阅 broker 队列（与 SSE `/stream` 同构），把事件渲染到终端。类 bash 行式 REPL：IDLE 用 `input()` 读输入、RUNNING 消费 broker 事件到结束（Ctrl+C 中断 cancel），运行中不做 steering。`-c` 按 `history.json` 的 mtime 找当前目录最新主 session 续接（加载历史、不回显）。不改动 agent/broker/工具层。详见 `doc/product/cli.md`。
* `agent.py` -- 核心 Agent 类。管理会话生命周期：构建 system 消息（env 块 + `SYSTEM_PROMPT` + 工作目录 `AGENTS.md` + skills 清单），与 LLM 流式对话，循环执行 tool calls，支持 plan/build 双模式切换、用户 steering、上下文压缩、subagent 派生（`task` 工具）和 VLM 图片输入。详见 `doc/product/context_building.md`、`doc/product/streaming.md`、`doc/product/subagent.md`、`doc/product/skills.md`。
* `messages.py` -- 类型化对话消息层（v2 内存表示）。`Message` 基类及 `SystemMessage`/`UserMessage`/`AssistantMessage`/`CompactMarker`/`Image`/`ToolCall`/`ToolResult` 子类，每个对象三向投影：`to_api_dicts`（发 LLM）、`to_storage_dict`（持久化 v2 JSON）、`to_frontend_dicts`（v1 兼容扁平 dict 给前端）。`AssistantMessage` 自包含一个 turn（thinking+content+tool_calls+tool_results），保证 steering 消息只能落在整个 turn 之后、API 消息序列永远合法。`from_v1` 把旧扁平格式惰性升级。详见 `doc/product/history.md`。
* `history.py` -- 对话历史管理。持久化到 v2 JSON（`{"version":2,"messages":[...]}`），内部用 `messages.py` 的类型对象。`get_api_messages` 从最后一个 `CompactMarker` 之后取消息、合并连续同 role 的 UserMessage（`<multi_message/>` 分隔，避免 API 报错）。
* `broker.py` - 流式事件总线（StreamBroker）。解耦 agent 运行与 HTTP 请求：事件广播到所有订阅者，维护当前 run 的回放缓冲和已提交历史长度（`persisted_len`），支持浏览器多开、关闭重开、切 tab 恢复。终态事件 `done`/`cancelled`/`error`/`interrupted` 推进 `persisted_len` 并清空缓冲。详见 `doc/product/streaming.md`。
* `tool_call.py` - 工具注册与分发。维护全局工具列表，两个**虚拟工具**定义（仅 LLM 可见，执行逻辑在 Agent 中）：`EXIT_PLAN_MODE_DEF`（`exit_plan_mode`，请求退出 plan 模式）和 `TASK_TOOL_DEF`（`task`，派生并行 subagent）；`execute_tool` 执行具体工具。
* `tools/tool.py` -- `Tool` 抽象基类（ABC），声明 `name`/`description`/`parameters` + 抽象 `execute` + `to_openai_tool`，所有具体工具继承它。
* `permission.py` - Agent 权限策略抽象。`AgentPermission` 基类承担两个职责：声明工具列表（`filter_definitions`）+ 拦截调用（`intercept`）。`tool_names` 声明**允许集**，通用 `intercept` 以它为唯一依据拦截调用；`advertised_tool_names` 声明**暴露集**，缺省值为允许集。主 agent 的两种模式暴露**完全相同的全量工具集**（`advertised_tool_names = ALL_TOOL_NAMES`，跨模式切换 tools 前缀一致、上下文缓存持续命中），模式差异由 `intercept` 与 `<modeswitch>` 提示词共同执行；subagent 角色保持缺省暴露集（暴露即允许）。子类区分两类生命周期：**模式**（`BuildModePermission`/`PlanModePermission`，主 agent 运行时热切换）vs **角色**（`ExploreSubagentPermission`/`GeneralSubagentPermission`，subagent 派生时固化）。`task` 工具仅主 agent 暴露（硬性限一层嵌套）。详见 `doc/product/plan_build_mode.md`。
* `compaction.py` -- 上下文压缩。`should_compact` 用上次 API 返回的真实 token 用量 + pending 消息估算，超 `max_context_tokens * compaction_trigger_ratio` 阈值时触发；`build_compact_prompt` 构建 kimi 风格第一人称 handoff 请求，自动附带 TODO 列表（不转录）。详见 `doc/product/compact.md`。
* `image_storage.py` -- 图片存储。`save_image` 把图片字节存到 `session_dir/images/<sha256前12位>.<ext>`，返回 `Image` 元数据；提供 MIME↔扩展名映射。供 `read` 工具读图片和用户上传图片复用。详见 `doc/product/vlm.md`。
* `config.py` -- 配置管理。首次运行自动从包内 `config_example/` 复制默认配置（仅 `config.yaml`）。
* `skills.py` -- Skill 发现与加载（Agent Skills 格式：`SKILL.md` + YAML frontmatter）。按优先级扫描：内置（`create-skill`，最低、磁盘同名可覆盖）→ trilobite（`.trilobite/skills`、`.agents/skills`、`~/.config/trilobite/skills`、`~/.agents/skills`、`skill_dirs`）→ opencode（`.opencode/{skill,skills}`）→ kimi（`.kimi-code/skills`、`$KIMI_CODE_HOME`）→ claude（`.claude/skills`），同名先到先得（trilobite > opencode > kimi > claude）；支持目录形式 `<name>/SKILL.md` 和平铺形式 `<name>.md`；`format_skill_listing` 渲染 `<available_skills>` 清单注入 system prompt（只含 name/description/path，正文由 `skill` 工具按需加载）。详见 `doc/product/skills.md`。
* `prompts.py` -- 所有提示词的代码常量，硬编码不可配置：`SYSTEM_PROMPT`、`COMPACTION_PROMPT`（压缩摘要）、`SUBAGENT_ROLE_PREFIX` + `SUBAGENT_ROLE_PROMPTS`（explore/general 角色）、`IMAGE_READ_PROMPT`（VLM）。
* `tokens.py` -- 简易 token 估算（字符级，区分 ASCII/CJK）。
* `file_access.py` -- 文件路径解析和安全检查（敏感文件过滤）。详见 `doc/product/file_access.md`。
* `file_discovery.py` - 文件发现（glob/grep 共用）：git 仓库用 `git ls-files` 尊重 .gitignore，否则 `os.walk` 跳过噪音目录；不依赖 ripgrep。
* `git_ops.py` - 文件管理器的 git 封装：`list_dir`（单目录条目 + porcelain 变更状态，被 ignore 目录不出现）、`list_branches`/`current_branch`、`show_base_content`（`git show <base>:<path>`，文件不在基线返回空）、`build_diff_rows`（difflib 全文件对比生成 DiffRow）。
* `tools/` -- 八个具体工具（均继承 `tools/tool.py`）：
  * `read.py` -- 读取文件（支持 start_line/limit_lines/limit_chars 分页）；`enable_vl` 时读图片存入 session images 目录并返回 `<image/>` marker
  * `glob.py` -- 按文件名模式查找（尊重 .gitignore，按 mtime 倒序）
  * `grep.py` -- 正则搜索文件内容（content/files_with_matches/count 模式、上下文行、glob/路径过滤）
  * `edit.py` -- 精确字符串替换（行尾归一化检测、replace_all、上下文 diff）
  * `write.py` -- 整文件创建/覆盖/追加（overwrite/append，自动建父目录）
  * `bash.py` -- 执行 shell 命令（进程组 + reader 线程逐行流式输出，输出默认截断尾部 100 行/10k 字符）
  * `todo.py` -- 任务列表管理（JSON 持久化在 session 目录 `todos.json`）
  * `skill.py` -- 按名加载 skill 的 SKILL.md 全文（`<skill_content>` 输出，含 base 目录说明）；调用时重新发现，会话中途新增的 skill 也可加载

## 前端 (`frontend/`)

Vue 3 + TypeScript，构建后输出到 `src/trilobite/static/`，由 FastAPI 直接 serve。

* `main.ts` -- 入口
* `App.vue` -- 根组件，布局 sidebar + chat + token bar；subagent 顶栏（返回父 session、类型标签、sealed 只读）；plan_exit/permission/subagent_permission 审批横幅；Tab 键切换 Plan/Build
* `store.ts` - 全局状态：sessions、chat items、SSE 订阅流处理（`init` 重建对话、事件驱动 isStreaming、断线自动重连）、plan mode、subagent 状态、VLM 开关；session 列表 3s 轮询
* `api.ts` -- HTTP API 封装（会话 CRUD、消息/图片、mode、permission、revert、interrupt、stream 订阅等）
* `types.ts` -- TypeScript 类型定义（SSEEvent 联合、HistoryMessage、SubagentChild、DiffRow 等）
* `components/` -- ChatView（窗口化渲染长历史）、ChatInput（自适应 textarea、`/compact` 补全、图片粘贴上传、模式切换）、SessionSidebar（会话列表 + subagent 子树 + 信息面板 + 文件管理器入口）、TokenBar、TurnBlock、ThinkingBlock（可折叠、活气泡 tail-f 跟随）、ToolEntry（task 工具显示 subagent 树、read 可折叠、diff 展示）、DiffView（分屏/统一双视图响应式切换）、UserMessage（编辑重发 revert、图片缩略图 + lightbox）、FileManager（文件管理器面板：查看/diff/编辑三视图，highlight.js 只读高亮）、FileTree（懒加载目录树，展开时按目录请求、git 状态徽章）
* `utils/markdown.ts` -- Markdown 渲染（marked + GFM，LaTeX 占位符保护）
* `utils/mathjax.ts` -- 数学公式渲染（懒加载 vendored MathJax）

## Plan/Build 双模式

Agent 有两种运行模式：
* **Plan 模式**（只读）：`edit`/`write` 被拦截（工具仍暴露但不执行）。可经 `exit_plan_mode` 请求切换；只能派生 `explore`（只读）subagent。
* **Build 模式**（全权限）：所有工具可用，可派生 `explore`/`general` subagent。

两种模式向 LLM 暴露**完全相同的工具集**，模式差异通过 `<modeswitch>` 提示词消息 + `permission.intercept` 执行层拦截实现，这样模式切换时 tools 前缀不变、缓存不全量失效。Agent 调用 `exit_plan_mode` 时前端展示审批横幅，用户批准后切换。按 **Tab** 可在两种模式间切换。模式通知作为 `is_mode_notification` 的 user 消息追加到历史末尾（run 时检查追加，保持 API 前缀单调增长命中缓存；首次 run 和 compaction 后也会注入）。详见 `doc/product/plan_build_mode.md`。

## Subagent

主 agent 通过虚拟 `task` 工具一次调用派生多个子 agent 并行运行（`asyncio.gather`），子 agent 上下文隔离、只拿主 agent 给的 prompt 不继承历史，结果聚合为 `<task_result>` 回填。Subagent 是**角色**非**模式**：`ExploreSubagentPermission`（只读）/ `GeneralSubagentPermission`（可编辑）派生时固化，无 `exit_plan_mode`；子 agent 工具白名单不含 `task`，硬性限制单层嵌套。子 agent 是有界任务，以总结结束不再响应；可被用户 steering 或 interrupt（中断后立即产出总结退出）。侧边栏树状展示父子 session，子 agent 权限请求全局可见。详见 `doc/product/subagent.md`。

## VLM 图片输入

`config.yaml` 设 `enable_vl: true` 开启。开启后前端出现图片按钮（多选 + 粘贴 + 缩略图 + lightbox）；用户消息图片以元数据存于 `session_dir/images/`、作为 OpenAI 兼容 `image_url` content part 发送。`read` 工具读到图片时返回 `<image/>` marker 并把图片挂到触发该调用的 user 消息上，下轮 LLM 可见。关闭后新图片不保存、旧图片不进 LLM 请求但保留在持久化历史。仅主 session 显示按钮、运行中禁用。详见 `doc/product/vlm.md`。

## 文件访问权限

`read`/`edit`/`write`/`glob`/`grep` 工具在 working_dir（存于 `session.json`）范围内操作；访问工作区以外路径时前端弹出权限请求横幅（Grant/Deny）。批准后目录加入 `additional_dirs` 并持久化到 `session.json`，重启保留。`bash` 不强制路径限制，靠系统提示词引导。敏感文件直接拒绝。详见 `doc/product/file_access.md`。

## 文件管理器

用户主动操作工作区文件的轻量 IDE 界面（仅主 session，sidebar 入口），与 agent 完全解耦：查看文件（highlight.js 高亮）、对比文件与指定 git 分支（默认 master，复用 `DiffView` 渲染 `DiffRow[]`）、textarea 编辑后直接落盘。文件树按目录懒加载（展开时请求 `fs/list`），git 仓库中被 ignore 的目录不出现，条目带 M/A/U/D 状态徽章。编辑保存不受 plan mode / agent 运行状态限制，不产生对话历史、不走 permission 审批（用户操作，与终端改文件同语义）；路径统一走 `resolve_file_path` 边界检查，二进制/超 512KB/敏感文件拒绝。详见 `doc/product/file_manager.md`。

## 上下文压缩

Token 超过 `compaction_trigger_ratio` 阈值时触发压缩：插入 `CompactMarker`（无内容裁剪标记）+ 重建的 SystemMessage，`get_api_messages` 从最后一个 marker 之后取消息发给 LLM，marker 之前仍在 `history.json` 供前端展示（前端历史与 API 上下文分离，不丢消息）。提示词采用 kimi 风格的第一人称 handoff note。TODO 列表自动附带，不转录。详见 `doc/product/compact.md`。

## 配置 (`src/trilobite/config_example/`)

* `config.yaml` -- `model`、`api_key`、`api_url`、`reasoning_effort`、`max_context_tokens`（上下文窗口）、`max_tokens`（单次输出上限）、`log_level`、`compaction_trigger_ratio`、`enable_vl`、`skill_dirs`（额外的 skill 搜索目录，相对路径基于工作目录，支持 `~` 展开）

提示词（系统提示词、压缩摘要、subagent 角色）不在配置里，而是硬编码在 `src/trilobite/prompts.py`，不可配置。

# 构建

一条命令打包成 pip 包（先构建前端，再打 wheel/sdist）：

```bash
./build.sh
```

`build.sh` 做两件事：

1. `cd frontend && npm ci && npm run build` -- 前端产物输出到 `src/trilobite/static/`（vite `outDir` 配置），含 `assets/`、`mathjax/`、`vendor/` 等。
2. `uv build` -- 生成 sdist + wheel 到 `dist/`（`trilobite_code-<ver>-py3-none-any.whl` 和 `.tar.gz`）。

## 数据文件如何进包

`src/trilobite/static/` 被 `.gitignore` 忽略且不在 git 追踪中，所以**不能**依赖 `include_package_data` 的 VCS 机制（会漏掉 static）。改用：

* `MANIFEST.in` 的 `recursive-include` 把 `src/trilobite/static/` 和 `src/trilobite/config_example/` 塞进 sdist（纯文件系统 glob，不读 `.gitignore`）。
* `pyproject.toml` 设 `include-package-data = true`，wheel 从 sdist 构建时一并带上这些数据文件。

`config_example/` 在包内（`src/trilobite/config_example/`），`config.py` 用 `Path(__file__).parent / "config_example"` 定位，`server.py` 同理用 `__file__` 定位 `static/`，所以装进 site-packages 后路径天然正确，后端代码无需感知安装位置。

## 安装与运行

```bash
uv tool install dist/trilobite_code-1.1.2-py3-none-any.whl
trilobite   # entry point，启动 uvicorn (0.0.0.0:2345)
```

首次运行会从包内 `config_example/` 把默认配置 seed 到 `~/.config/trilobite/`。

# 风格和注意事项

## 项目管理

注意：
* 永远先拉master，基于新的master开分支，不要在master分支中编写代码。
* 一般来说我用trilobite开发trilobite，所以你其实就运行在这个trilobite里面，这个进程占据了2345端口，当功能开发完后你可以叫我来重启进程。
* gh已经配备，可以查看github的issues和PRs。
* 编辑完成后，记得创建github PR。

流程：
* 进行任何修改前，扫描产品文档`doc/product`
* 进行修改前，先拉最新的master分支，然后专门的特性分支
* 修改后，如果流程发生变更，记得更新产品文件：每次修改agent的工作方式后，需要在`doc/product`的相应文件中说明
* 更新完功能后，修改pyproject.toml中包版本的第三个数字。比如当前是1.1.2，那就改为1.1.3。注意，版本号需要相对于最新版master进行bump，避免一个PR内多次bump。
* 完成后提交PR

## 后端

* 3.3后的现代python不应该用__init__.py
* 全篇采用绝对引用`from src.trilobite.xxxx import yyyy`，禁止使用相对引用

## 前端

* 不要去整个读取那些编译出来的js文件，超级大，会耗尽上下文

## 产品文档

* 产品文档描述最终事实，而不要描述过程。避免“是xxx而不是xxx”，或者“之前是xxx，现在是xxx”一类的描述。