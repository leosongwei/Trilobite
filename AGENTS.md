我们来造一个简单的coding agent。

# 项目架构

Trilobite 是一个 coding agent，通过 OpenAI 兼容 API 调用 LLM（默认 DeepSeek），分为 **后端 (Python/FastAPI)** 和 **前端 (Vue 3/TypeScript)** 两部分。
## 后端 (`src/trilobite/`)

* `server.py` - FastAPI 应用入口。会话级 REST API：`POST /api/sessions/{name}/message` 启动/转向 agent（返回 JSON，agent 作为独立 asyncio task 运行，关闭浏览器不取消）、`GET /api/sessions/{name}/stream` SSE 订阅实时输出；另有会话 CRUD、mode 切换、permission/plan_exit 审批、`/revert`、`/compact`、`/interrupt`、`/wake`（唤醒 sleep_until 挂起的会话）、additional_dirs 增删、`/history`、`/info`、图片 serve、文件管理器 `fs/list`/`fs/file`/`fs/diff`/`fs/file`(PUT)、项目 `projects` CRUD 与 session 归属等端点；startup 装配 `TimerService`（加载 `session.json` 的 `sleep_until` 挂起字段、每秒 tick 到点唤醒）；挂载静态前端文件。token 访问控制：启动时生成随机 key（写入配置目录 `token` 文件并打印带 `?token=` 的链接），`/api/auth/status`、`/api/auth/login` 换取 HttpOnly cookie，其余 `/api/*` 由中间件校验。`main()` 用 argparse 分发：`-t`/`-c` 进入 CLI 模式，默认 `-s` 启动 uvicorn（监听地址/端口由配置 `host`/`port` 决定，默认 127.0.0.1:2345），关闭 access log。详见 `doc/product/security.md`、`doc/product/file_manager.md`、`doc/product/projects.md`。
* `cli.py` -- 命令行交互模式（`trilobite -t [dir]` 新建 / `-c` 续接当前目录最新主 session / `-s` 服务器为默认）。纯订阅者 + 渲染器：实例化 `Agent`、`attach_subscriber()` 订阅 broker 队列（与 SSE `/stream` 同构），把事件渲染到终端。类 bash 行式 REPL：IDLE 用 `input()` 读输入、RUNNING 消费 broker 事件到结束（Ctrl+C 中断 cancel），运行中不做 steering。`-c` 按 `history.json` 的 mtime 找当前目录最新主 session 续接（加载历史、不回显）。不改动 agent/broker/工具层。详见 `doc/product/cli.md`。
* `agent.py` -- 核心 Agent 类。管理会话生命周期：构建 system 消息（env 块 + `SYSTEM_PROMPT` + 工作目录 `AGENTS.md` + skills 清单），与 LLM 流式对话，循环执行 tool calls，支持 plan/build 双模式切换、用户 steering、上下文压缩、subagent 派生（`task` 工具）和 VLM 图片输入。详见 `doc/product/context_building.md`、`doc/product/streaming.md`、`doc/product/subagent.md`、`doc/product/skills.md`。
* `messages.py` -- 类型化对话消息层（v3 扁平表示，与 completions API 消息序列同构）。`Message` 基类（带 `_id`，紧凑 uuid，revert 按 id 寻址）及 `SystemMessage`/`UserMessage`/`ModelMessage`（think/content/calls）/`ToolResults`/`CompactMarker`/`Image`/`ToolCall`/`ToolResult` 类型，每个对象三向投影：`to_api_dicts`（发 LLM）、`to_storage_dict`（v3 JSON：`type` 字段 + `id`，tool_calls 存 OpenAI 形状）、`to_frontend_dicts`（role-based 扁平 dict 给前端，带 `id`）。一条 model turn 在扁平列表里是 `ModelMessage` + 紧跟的 `ToolResults` 两条；"整批工具结果先于任何后续 user 消息"的顺序不变量由 `history.py` 的写入原语保证，而非对象嵌套。`from_v1`/`from_v2`/`message_from_storage` 三版 loader 把旧格式惰性升级到 v3（load 时补发 id，首次 save 写 v3）。详见 `doc/product/history.md`。
* `history.py` -- 对话历史管理。`MessageList` 是扁平事实源：语义原语 `append`/`append_model`/`close_model`/`insert_result`/`insert_results`/`truncate_at(message_id)` 封装顺序不变量，`get_api_messages` 从最后一个 `CompactMarker` 之后取消息、合并连续同 role 的 UserMessage（`<multi_message/>` 分隔，避免 API 报错）；`TurnsView` 是 turn 分组视图（`turns` 折叠、`find_user` 按 id 定位），领域操作拆解成 MessageList 原语，从不直接改列表。
* `broker.py` - 流式事件总线（StreamBroker）。解耦 agent 运行与 HTTP 请求：事件广播到所有订阅者，维护当前 run 的回放缓冲和已提交历史长度（`persisted_len`），支持浏览器多开、关闭重开、切 tab 恢复。终态事件 `done`/`cancelled`/`error`/`interrupted` 推进 `persisted_len` 并清空缓冲。详见 `doc/product/streaming.md`。
* `tool_call.py` - 工具注册与分发。维护全局工具列表，三个**虚拟工具**定义（仅 LLM 可见，执行逻辑在 Agent 中）：`EXIT_PLAN_MODE_DEF`（`exit_plan_mode`，请求退出 plan 模式）、`TASK_TOOL_DEF`（`task`，派生并行 subagent）和 `SLEEP_UNTIL_DEF`（`sleep_until`，挂起会话到指定时间）；`execute_tool` 执行具体工具。
* `tools/tool.py` -- `Tool` 抽象基类（ABC），声明 `name`/`description`/`parameters` + 抽象 `execute` + `to_openai_tool`，所有具体工具继承它。
* `permission.py` - Agent 权限策略抽象。`AgentPermission` 基类承担两个职责：声明工具列表（`filter_definitions`）+ 拦截调用（`intercept`）。`tool_names` 声明**允许集**，通用 `intercept` 以它为唯一依据拦截调用；`advertised_tool_names` 声明**暴露集**，缺省值为允许集。主 agent 的两种模式暴露**完全相同的全量工具集**（`advertised_tool_names = ALL_TOOL_NAMES`，跨模式切换 tools 前缀一致、上下文缓存持续命中），模式差异由 `intercept` 与 `<modeswitch>` 提示词共同执行；subagent 角色保持缺省暴露集（暴露即允许）。子类区分两类生命周期：**模式**（`BuildModePermission`/`PlanModePermission`，主 agent 运行时热切换）vs **角色**（`ExploreSubagentPermission`/`GeneralSubagentPermission`，subagent 派生时固化）。`task` 工具仅主 agent 暴露（硬性限一层嵌套）。详见 `doc/product/plan_build_mode.md`。
* `compaction.py` -- 上下文压缩。`should_compact` 用上次 API 返回的真实 token 用量 + pending 消息估算，超 `max_context_tokens * compaction_trigger_ratio` 阈值时触发；`build_compact_prompt` 构建 kimi 风格第一人称 handoff 请求，自动附带 TODO 列表（不转录）。详见 `doc/product/compact.md`。
* `timer.py` -- 会话定时器（sleep_until）。`TimerService` 持有全部挂起（内存 pending 表 + `session.json` 的 `sleep_until` 字段，epoch 秒），每秒 tick 到点唤醒：追加 `⏰ 定时唤醒（<当前时间>）` 合成 user 消息并启动新 run，同一会话续跑；`parse_sleep_until` 解析相对/绝对时间（错误附当前时间），服务重启后过期目标立即补唤醒。详见 `doc/product/timer.md`。
* `image_storage.py` -- 图片存储。`save_image` 把图片字节存到 `session_dir/images/<sha256前12位>.<ext>`，返回 `Image` 元数据；提供 MIME↔扩展名映射。供 `read` 工具读图片和用户上传图片复用。详见 `doc/product/vlm.md`。
* `config.py` -- 配置管理。首次运行自动从包内 `config_example/` 复制默认配置（仅 `config.yaml`）。
* `skills.py` -- Skill 发现与加载（Agent Skills 格式：`SKILL.md` + YAML frontmatter）。按优先级扫描：内置（`create-skill`，源码在包内 `builtin_skills/`，最低、磁盘同名可覆盖）→ `.agents`（`.agents/skills`、`~/.agents/skills`，跨工具共享、最高）→ trilobite（`.trilobite/skills`、`~/.config/trilobite/skills`、`skill_dirs`）→ opencode（`.opencode/{skill,skills}`）→ kimi（`.kimi-code/skills`、`$KIMI_CODE_HOME`）→ claude（`.claude/skills`），同名先到先得（`.agents` > trilobite > opencode > kimi > claude）；支持目录形式 `<name>/SKILL.md` 和平铺形式 `<name>.md`；`format_skill_listing` 渲染 `<available_skills>` 清单注入 system prompt（只含 name/description/path，正文由 `skill` 工具按需加载）。详见 `doc/product/skills.md`。
* `builtin_skills/` -- 内置 skill 的源码目录（标准 `<name>/SKILL.md`，随包分发，经 `skills.py` 的 `_load_builtin_skills` 加载，与磁盘 skill 同解析路径）。
* `prompts.py` -- 所有提示词的代码常量，硬编码不可配置：`SYSTEM_PROMPT`、`COMPACTION_PROMPT`（压缩摘要）、`SUBAGENT_ROLE_PREFIX` + `SUBAGENT_ROLE_PROMPTS`（explore/general 角色）、`IMAGE_READ_PROMPT`（VLM）。
* `tokens.py` -- 简易 token 估算（字符级，区分 ASCII/CJK）。
* `file_access.py` -- 文件路径解析和安全检查（敏感文件过滤）。详见 `doc/product/file_access.md`。
* `file_discovery.py` - 文件发现（glob/grep 共用）：git 仓库用 `git ls-files` 尊重 .gitignore，否则 `os.walk` 跳过噪音目录；不依赖 ripgrep。
* `projects.py` - 项目（session 分组）持久化：`projects.json`（与 session 目录同级）存储项目列表（id/name/working_dir/created_at），提供增删查；session 归属通过 `session.json` 的 `project_id` 字段记录，删除项目只解除归属不删 session。详见 `doc/product/projects.md`。
* `git_ops.py` - 文件管理器的 git 封装：`list_dir`（单目录条目 + porcelain 变更状态，被 ignore 目录不出现）、`list_branches`/`current_branch`、`show_base_content`（`git show <base>:<path>`，文件不在基线返回空）、`build_diff_rows`（difflib 全文件对比生成 DiffRow）。
* `tools/` -- 八个具体工具（均继承 `tools/tool.py`）：
  * `read.py` -- 读取文件（支持 start_line/limit_lines/limit_chars 分页）；`enable_vl` 时读图片存入 session images 目录并返回 `<image/>` marker
  * `glob.py` -- 按文件名模式查找（尊重 .gitignore，按 mtime 倒序）
  * `grep.py` -- 正则搜索文件内容（content/files_with_matches/count 模式、上下文行、glob/路径过滤）
  * `edit.py` -- 精确字符串替换（行尾归一化检测、replace_all、上下文 diff）
  * `write.py` -- 整文件创建/覆盖/追加（overwrite/append，自动建父目录）
  * `bash.py` -- 执行 shell 命令（进程组 + reader 线程逐行流式输出，输出默认截断尾部 100 行/10k 字符；Linux 上默认套 bubblewrap 沙箱，工作区 + 授权目录外只读，被拒时附 `[sandbox]` 提示引导走 read 审批）
  * `todo.py` -- 任务列表管理（JSON 持久化在 session 目录 `todos.json`）
  * `skill.py` -- 按名加载 skill 的 SKILL.md 全文（`<skill_content>` 输出，含 base 目录说明）；调用时重新发现，会话中途新增的 skill 也可加载

## 前端 (`frontend/`)

Vue 3 + TypeScript，构建后输出到 `src/trilobite/static/`，由 FastAPI 直接 serve。

* `main.ts` -- 入口
* `App.vue` -- 根组件，布局 sidebar + chat + token bar；subagent 顶栏（返回父 session、类型标签、sealed 只读）；plan_exit/permission/subagent_permission 审批横幅；Tab 键切换 Plan/Build
* `store.ts` - 全局状态：sessions、chat items、SSE 订阅流处理（`init` 重建对话、事件驱动 isStreaming、断线自动重连）、plan mode、subagent 状态、VLM 开关；session 列表 3s 轮询
* `api.ts` -- HTTP API 封装（会话 CRUD、消息/图片、mode、permission、revert、interrupt、stream 订阅等）
* `types.ts` -- TypeScript 类型定义（SSEEvent 联合、HistoryMessage、SubagentChild、DiffRow 等）
* `components/` -- ChatView（窗口化渲染长历史）、ChatInput（自适应 textarea、`/compact` 补全、图片粘贴上传、模式切换）、SessionSidebar（会话列表 + 项目分组 + subagent 子树 + 信息面板 + 文件管理器入口）、TokenBar、TurnBlock、ThinkingBlock（可折叠、活气泡 tail-f 跟随）、ToolEntry（task 工具显示 subagent 树、read 可折叠、diff 展示）、DiffView（分屏/统一双视图响应式切换）、UserMessage（编辑重发 revert、图片缩略图 + lightbox）、FileManager（文件管理器面板：查看/diff/编辑三视图，highlight.js 只读高亮）、FileTree（懒加载目录树，展开时按目录请求、git 状态徽章）
* `utils/markdown.ts` -- Markdown 渲染（marked + GFM，LaTeX 占位符保护）
* `utils/mathjax.ts` -- 数学公式渲染（懒加载 vendored MathJax）

## Plan/Build 双模式

Agent 有两种运行模式：
* **Plan 模式**（只读）：`edit`/`write` 被拦截（工具仍暴露但不执行）。可经 `exit_plan_mode` 请求切换；只能派生 `explore`（只读）subagent。
* **Build 模式**（全权限）：所有工具可用，可派生 `explore`/`general` subagent。

两种模式向 LLM 暴露**完全相同的工具集**，模式差异通过 `<modeswitch>` 提示词消息 + `permission.intercept` 执行层拦截实现，这样模式切换时 tools 前缀不变、缓存不全量失效。Agent 调用 `exit_plan_mode` 时前端展示审批横幅，用户批准后切换。按 **Tab** 可在两种模式间切换。模式通知作为 `is_mode_notification` 的 user 消息追加到历史末尾（run 时检查追加，保持 API 前缀单调增长命中缓存；首次 run 和 compaction 后也会注入）。详见 `doc/product/plan_build_mode.md`。

## Subagent

主 agent 通过虚拟 `task` 工具一次调用派生多个子 agent 并行运行（`asyncio.gather`），子 agent 上下文隔离、只拿主 agent 给的 prompt 不继承历史，结果聚合为 `<task_result>` 回填。Subagent 是**角色**非**模式**：`ExploreSubagentPermission`（只读）/ `GeneralSubagentPermission`（可编辑）派生时固化，无 `exit_plan_mode`；子 agent 工具白名单不含 `task`，硬性限制单层嵌套。子 agent 是有界任务，以总结结束不再响应；可被用户 steering 或 interrupt（中断后立即产出总结退出）。侧边栏树状展示父子 session，子 agent 权限请求全局可见。详见 `doc/product/subagent.md`。

## Timer（sleep_until 定时挂起）

主 agent 的虚拟 `sleep_until` 工具把**当前会话**挂起到指定时间（相对时长 `+30m` 或绝对本地时间 `YYYY-MM-DD HH:MM` 两种格式，5 秒~365 天）。模型视角它是一个执行得特别慢的普通工具：调用**不产生结果**，结果延迟到唤醒时构造（文案标注准点/提前/迟到/被打断，携带当前时间）并插入本批 ToolResults，与兄弟调用的结果一起在唤醒后的第一个请求里交给模型——无合成 user 消息。同一批内 `sleep_until` 总是最后执行（稳定重排），挂起在其他调用完成后开始。本轮 run 结束（挂起零 token 消耗），到点由 `TimerService` 触发唤醒 run，模型在**同一上下文**续跑。挂起状态持久化在 `session.json` 的 `sleep_until` 字段，重启后重载、停机期间错过的唤醒立即补触发；延迟结果靠扫描历史定位（无内存态依赖）。挂起中的会话侧边栏显示蓝点（`has_sleep`）、排序置顶，顶栏有挂起横幅 + 立即唤醒按钮；**停止按钮保持可按**（按下即中断挂起——交付"已中断"工具结果、不启动唤醒 run，会话回到等待输入，与普通工具调用被停止同构）；用户发消息（含运行中 steer）即打断挂起、当轮响应，是否再睡由模型自决。仅主 agent（build/plan 双模式一致暴露，缓存稳定）可用，subagent 与 CLI 不可用。取代已移除的定时 subagent（cron），旧规格存档于 `doc/product/archived/scheduled_subagent.md`。详见 `doc/product/timer.md`。

## VLM 图片输入

`config.yaml` 设 `enable_vl: true` 开启。开启后前端出现图片按钮（多选 + 粘贴 + 缩略图 + lightbox）；用户消息图片以元数据存于 `session_dir/images/`、作为 OpenAI 兼容 `image_url` content part 发送。`read` 工具读到图片时返回 `<image/>` marker 并把图片挂到触发该调用的 user 消息上，下轮 LLM 可见。关闭后新图片不保存、旧图片不进 LLM 请求但保留在持久化历史。仅主 session 显示按钮、运行中禁用。详见 `doc/product/vlm.md`。

## 文件访问权限

`read`/`edit`/`write`/`glob`/`grep` 工具在 working_dir（存于 `session.json`）范围内操作；访问工作区以外路径时前端弹出权限请求横幅（Grant/Deny）。批准后目录加入 `additional_dirs` 并持久化到 `session.json`，重启保留。config 的 `allowed_dirs` 配置全局固定授权目录（所有会话生效、UI 不可移除，侧边栏灰色展示）。`bash` 在 Linux 上默认套 bubblewrap 沙箱（config `bash_sandbox`：auto/on/off），沙箱内仅工作区 + 授权目录（全局 + 按会话）可写。敏感文件直接拒绝。详见 `doc/product/file_access.md`。

## 文件管理器

用户主动操作工作区文件的轻量 IDE 界面（仅主 session，sidebar 入口），与 agent 完全解耦：查看文件（highlight.js 高亮）、对比文件与指定 git 分支（默认 master，复用 `DiffView` 渲染 `DiffRow[]`）、textarea 编辑后直接落盘。文件树按目录懒加载（展开时请求 `fs/list`），git 仓库中被 ignore 的目录不出现，条目带 M/A/U/D 状态徽章。编辑保存不受 plan mode / agent 运行状态限制，不产生对话历史、不走 permission 审批（用户操作，与终端改文件同语义）；路径统一走 `resolve_file_path` 边界检查，二进制/超 512KB/敏感文件拒绝。详见 `doc/product/file_manager.md`。

## 上下文压缩

Token 超过 `compaction_trigger_ratio` 阈值时触发压缩：插入 `CompactMarker`（无内容裁剪标记）+ 重建的 SystemMessage，`get_api_messages` 从最后一个 marker 之后取消息发给 LLM，marker 之前仍在 `history.json` 供前端展示（前端历史与 API 上下文分离，不丢消息）。提示词采用 kimi 风格的第一人称 handoff note。TODO 列表自动附带，不转录。详见 `doc/product/compact.md`。

## 配置 (`src/trilobite/config_example/`)

* `config.yaml` -- `model`、`api_key`、`api_url`、`reasoning_effort`、`max_context_tokens`（上下文窗口）、`max_tokens`（单次输出上限）、`log_level`、`compaction_trigger_ratio`、`enable_vl`、`host`（服务监听地址，默认 127.0.0.1 仅本机可访问）、`port`（服务监听端口）、`skill_dirs`（额外的 skill 搜索目录，相对路径基于工作目录，支持 `~` 展开）、`allowed_dirs`（全局固定授权目录，所有会话默认可访问，不走权限审批、UI 不可移除，支持 `~` 展开、相对路径基于各会话工作目录解析）、`max_stream_retries`（单回合 LLM 流式请求最大尝试次数，含首次，默认 10：任意 HTTP 错误/流无完成信号断开/空完成（无正文无工具调用）都丢弃部分输出重发，详见 `doc/product/llm_transport.md`）

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
trilobite   # entry point，启动 uvicorn（默认 127.0.0.1:2345）
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

## 发布release

一般用户在github上合并PR到master后会需要发布release。

1. git checkout master && git pull，就应该能看到分支合并了
2. 基于master分支全量构建
3. gh上传构建产物发布release
4. 询问用户是否安装新版

## 后端

* 3.3后的现代python不应该用__init__.py
* 全篇采用绝对引用`from src.trilobite.xxxx import yyyy`，禁止使用相对引用

## 前端

* 不要去整个读取那些编译出来的js文件，超级大，会耗尽上下文

## 产品文档

* 产品文档描述最终事实，而不要描述过程。避免“是xxx而不是xxx”，或者“之前是xxx，现在是xxx”一类的描述。