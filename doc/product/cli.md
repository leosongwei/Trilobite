# CLI 模式（命令行交互）

> 状态：**实现规格**。本文是 CLI 模式的产品规格，据此实现。

## 一、概述

Trilobite 目前只有 web 模式：启动 uvicorn 服务，前端 Vue 应用通过 SSE 订阅 agent 输出。本功能增加一个**不启动服务器**的命令行交互模式，用 `-t`（新建会话）或 `-c`（续接当前目录最新会话）选项触发，以**命令执行目录**作为 agent 的 working dir，提供一个**类 bash** 的行式 REPL 界面（不是 opencode 那种 ncurses 全屏 TUI）。

核心价值：在终端里快速起一个 coding agent 会话，无需开浏览器、无需占端口；适合 SSH 远程、无头环境、脚本化场景。

### 设计原则

- **复用，不重写**：CLI 不另起一套 agent 逻辑，而是直接实例化 `Agent`，像 SSE 端点一样 `attach_subscriber()` 订阅 `StreamBroker` 队列，把事件渲染到终端。agent / broker / 工具 / 权限 / 压缩 / subagent 全部原样复用，CLI 只是一个新的「订阅者 + 渲染器」。
- **类 bash，非 TUI**：行式输入输出，不接管全屏、不画边框面板、不做 ncurses。输出和输入在同一行流上自然流动，能 pipe、能滚动。
- **颜色区分语义**：用 ANSI 颜色区分不同信息源，不靠框线布局。

### v1 范围（本次实现）

- ✅ `trilobite -s` 启动 web 服务器（默认，无参等价于 `-s`）。
- ✅ `trilobite -t [working_dir]` CLI 新建 session，working dir = 指定目录或 cwd。
- ✅ `trilobite -c` CLI 续接当前目录（cwd）最新的 session；无历史 session 时退化为新建。
- ✅ `-t` 以 cwd（或指定目录）为 working dir 新建 session。
- ✅ 类 bash REPL：输入消息 -> agent 运行 -> 流式输出 -> 输入下一条。
- ✅ 彩色输出（见第四节）。
- ✅ inline diff 渲染（复用 web 端 inline 版本的 diff 行结构）。
- ✅ readline 行编辑：退格 / 方向键 / 行首行尾等编辑交给 readline（`import readline`），不再依赖内核 cooked 模式（修复方向键/退格出乱码的问题）。
- ✅ `/stop`：仅在 IDLE 时提示「未运行」（运行中不读 stdin，要中断用 Ctrl+C）。
- ✅ 权限请求 / plan 退出请求：交互式 y/n 提示。
- ✅ `/exit`、`/quit`、Ctrl+D 退出。
- ✅ Ctrl+C：RUNNING 时中断当前 run 回 IDLE，IDLE 时退出 CLI。

### v1 不做的事（non-goals）

- ❌ **Markdown 解析**：终端原样输出纯文本，不做 Markdown / 代码块 / 公式渲染。
- ❌ **运行中 steering（中途引导）**：v1 采用 bash 语义--前台命令运行时不读输入，要中断就 Ctrl+C。改用 readline 的 `input()` 后无法在运行中多路复用 stdin，故放弃 steering（换取退格/方向键正常工作）。
- ❌ **命令补全**：readline 已接入，但 v1 不做 Tab 补全（后续可在 readline 上加）。
- ❌ **subagent 交互**：不切换 / 查看子 session、不单独中断某个 subagent。subagent 自然跑完，或由 Ctrl+C（`cancel`）连带取消（与 web 端主 session stop 行为一致）。subagent 的内部 thinking / 工具调用**不**在 CLI 主界面展示，只展示「启动 / 退出」两行（见第六节）。
- ❌ **历史回显**：`-c` 续接时加载历史供 agent 保持上下文，但终端**不回显**历史消息（只显示一条 `resumed` 提示，从当前继续）。完整历史仍可在 web 端查看。
- ❌ **ncurses / 全屏 TUI**：不接管终端、不做分屏面板。
- ❌ **plan/build 模式切换**（Tab）：v1 不支持运行中切换模式（沿用 session 创建时的 build 模式）。

## 二、入口与启动

### 命令行

```
trilobite                # 启动 web 服务器（默认，等价于 -s）
trilobite -s             # 启动 web 服务器
trilobite -t             # CLI 新建 session，working dir = cwd
trilobite -t <dir>       # CLI 新建 session，working dir = <dir>
trilobite -c             # CLI 续接当前目录最新的 session
```

`server.py:main()` 用 `argparse` 解析参数，`-s` / `-t` / `-c` 互斥：

| 参数 | 说明 |
|---|---|
| `-s`, `--server` | 启动 web 服务器（默认） |
| `-t`, `--cli` | CLI 新建 session |
| `-c`, `--continue` | CLI 续接当前目录（cwd）最新的 session |
| `[working_dir]`（位置参数，可选） | `-t` 模式的 working dir，默认 cwd；`-c` 忽略，固定用 cwd |

无参等价于 `-s`，走 `uvicorn.run(...)` 路径，**完全向后兼容**。

### 启动流程（CLI 模式）

`-t`（新建）与 `-c`（续接）共用 REPL，区别仅在 session 的获取：

**`-t` 新建**：
1. `init_config()` 加载配置（与 web 模式共用 `~/.config/trilobite/config.yaml`）。
2. 在 `get_sessions_dir()` 下新建一个 session 目录（`uuid4().hex`），写 `session.json`（`working_dir` 解析为绝对路径、`plan_mode: false`、`additional_dirs: []`、`created_at`、`model` = 默认模型名）。
3. 实例化 `Agent`（与 `POST /api/sessions` 相同的参数：`name`、`working_dir`、`session_dir`、`config`、`registry`、`model_name`），回写 `session_id`。
4. `await agent.attach_subscriber()` 拿到事件队列 + `init` 快照（新 session 历史为空）。
5. 进入 REPL 主循环（见第三节）。

**`-c` 续接**：
1. `init_config()`，取 `cwd`。
2. 扫描 `get_sessions_dir()` 下所有 `session.json`，过滤掉 subagent session（`subagent_type` 非空）和相对路径 `working_dir`，匹配 `Path(working_dir).resolve() == cwd`，按 `history.json` 的 mtime（最后一次存盘时间，回退 `created_at`）取最新。
3. 无匹配则退化新建（打印 `无历史 session，新建`），流程同 `-t`。
4. 有匹配则实例化 `Agent`（复用其 `session_id` 与保存的 `model`，`Agent` 从 `history.json` 加载历史），恢复 `plan_mode` / `additional_dirs`。
5. `attach_subscriber()` 拿队列 + 快照（**不回显历史**，只打印一条 `resumed · <name> · <working_dir>` 提示）。
6. 进入 REPL 主循环。

> 「最新 session」按 `history.json` 的 mtime（最后一次存盘时间）排序，回退 `created_at`，web 与 CLI 的所有活动都自动反映，无需额外维护时间字段。session 创建逻辑与 `POST /api/sessions` 端点一致，CLI 只是把「HTTP 请求驱动」换成「stdin 驱动」。

> 两种模式进入 REPL 时都先打印一行版本横幅 `Trilobite <version>`（与 web 启动横幅共用 `src/trilobite/version.py` 的 `get_version()`），再打印 banner 提示行（`# trilobite cli · <working_dir>` 或 `# resumed · <name> · <working_dir>`）。

## 三、交互模型：状态机

CLI 是一个两状态循环：

```
        ┌──────────────────────────────────┐
        │  IDLE（空闲，等待输入）            │
        │  readline input() 读一行（蓝色 ❯） │
        └──────────────────────────────────┘
          │ 输入消息（非命令）
          │ start(agent) ───────────────────────┐
          ▼                                      ▼
        ┌──────────────────────────────────┐
        │  RUNNING（运行中）                 │
        │  只消费 broker 事件并渲染：        │
        │    broker 事件 -> 渲染             │
        │    Ctrl+C     -> cancel 回 IDLE   │
        │  （不读 stdin，无 steering）       │
        └──────────────────────────────────┘
          │ 收到 done/cancelled/error/interrupted
          └──────────────────────────────────┘
          ▼
        回到 IDLE
```

### IDLE 状态

用 `input()` 读取一行（`import readline` 后退格/方向键/行编辑由 readline 处理，不再依赖内核 cooked 模式）。提示符为蓝色 `❯ `，其 ANSI 码用 readline 的 `\001..\002` 标记包裹，避免光标位置算错：

| 输入 | 行为 |
|---|---|
| 空行 | 重新提示（忽略） |
| `/stop` | 未运行，打印 dim `（未运行）` |
| `/exit`、`/quit` | 退出 CLI |
| Ctrl+D（EOF） | 退出 CLI |
| Ctrl+C | 退出 CLI |
| 其他非空 | `await agent.start(text)`，进入 RUNNING |

**人类输入蓝色**：IDLE 时无并发输出，输入回车后用 ANSI 把该行重渲染为蓝色（终端先以默认色回显，回车后 `\033[A\033[2K` 上移清行，重写蓝色 `❯ <text>`）。仅 IDLE 做此重渲染（无并发，安全；单行输入假设）。

### RUNNING 状态

进入 RUNNING 后，**只消费 broker 事件**，不读 stdin（无 steering、无 `/stop`）。循环 `await queue.get()` 取事件渲染：

- **broker 事件**：渲染（见第四节）。`permission_request` / `plan_exit_request` / `subagent_permission_request` 时，此刻 agent 阻塞在 `Event.wait()`、无并发输出，直接 `input()` 读 y/n 再 resolve（见第五节）。`done` / `cancelled` / `error` / `interrupted` 时结束本 run，回 IDLE。
- **Ctrl+C（SIGINT）**：由 `loop.add_signal_handler(signal.SIGINT, agent.cancel)` 处理，消费者继续从 queue 取到 `cancelled` 终结事件后回 IDLE（不退出）。

RUNNING 期间**不显示提示符**（类 bash：命令运行时不显示新提示），也**不读 stdin**（要中断就 Ctrl+C）。

> **为什么用 readline `input()` 而非常驻 reader 协程**：旧实现用 `loop.connect_read_pipe` + `asyncio.StreamReader` + 一个 `stdin_pump` 协程把 stdin 行塞进队列，RUNNING 用 `asyncio.wait` 多路复用 broker 事件 / stdin / Ctrl+C--这支持运行中 steering，但 stdin 仍处于内核 cooked 模式，退格/方向键在部分终端失效并出乱码控制字符（issue #40）。改用 `input()` 后 readline 接管终端（raw 模式 + 自身编辑器），退格/方向键/历史正常工作。代价是 `input()` 是同步阻塞调用、无法在运行中多路复用 stdin，故放弃 steering（bash 语义：前台命令运行时不读输入）。IDLE 期间 `input()` 阻塞事件循环无妨（无其他 task 在跑）。Ctrl+C 的处理分两种：IDLE / 权限提示时由 Python 默认 SIGINT handler 抛 `KeyboardInterrupt`（`input()` 可捕获）；RUNNING 时由 `loop.add_signal_handler(SIGINT, agent.cancel)` 回调处理（事件循环在跑，回调能执行）。注意权限提示的 `input()` 期间需临时 `remove_signal_handler` 切回默认 handler，否则 Ctrl+C 会被吞--readline 只调用已安装的 Python 信号 handler，而 asyncio 的 handler 仅排一个 loop 回调，在 `input()` 阻塞事件循环时无法执行。

### 退出

`/exit`、`/quit` 或 Ctrl+D：若正在运行先 `agent.cancel()`，等待 run 收尾，然后退出进程。不做额外清理（session 历史已落盘）。

## 四、事件渲染与配色

CLI 订阅 broker 队列，对每个事件按下表渲染。颜色仅在 stdout 是 tty 时启用（`sys.stdout.isatty()`），否则纯文本。

### 配色总表

| 信息源 | 颜色 | 说明 |
|---|---|---|
| 人类输入 | **蓝色** | IDLE 提示符 `❯ ` 与输入文本 |
| 思维链（thinking） | **灰色**（`#6e7681`） | reasoning 增量 |
| LLM 正文（text） | **白色** | 模型回复正文增量 |
| 工具调用 | **橘色** | `tool_start` 一行（`[<tool>: <args>]`，与 web 同色 `#ce9178`） |
| 工具输出 | **白色** | bash 实时输出 / 工具结果 |
| diff 新增行 | **绿色** `+` | inline diff |
| diff 删除行 | **红色** `-` | inline diff |
| diff 上下文 | **dim 灰** | inline diff 未改动行 |
| subagent 启动/退出 | **dim 灰** | 多行 |
| 错误 | **红色** | error 事件 |
| 状态/压缩 | **黄色 dim** | status / compact 横幅 |
| token 用量 | **dim 灰** | usage 事件，一行 |

> 「白色」：工具输出（`tool_output` / `tool_result` 正文）用 `\033[37m` **强制白色**（用户终端默认前景色各异，不依赖默认）；LLM 正文（`text`）沿用终端默认前景色。

### 逐事件渲染

| 事件 | 渲染 |
|---|---|
| `init` | 跳过（新 session 历史为空） |
| `user` | 跳过（人类输入已由输入侧显示，避免重复） |
| `turn` | 跳过 |
| `thinking` | 流式原样追加，灰色（`#6e7681`） |
| `text` | 流式原样追加，白色 |
| `tool_stream` | 跳过（工具调用在 `tool_start` 时一次性显示完整参数，不逐字符流式） |
| `tool_start` | 一行橘色 `[<tool>: <args>]`。见下「工具调用格式」 |
| `tool_output` | 逐行流式追加（bash stdout/stderr），白色，每行末补 `\n` |
| `tool_result` | 见下「工具结果渲染」 |
| `usage` | 一行 dim：`Tokens: <N> / <max> (<pct>%)`（与 web 端 TokenBar 同格式，千分位逗号 + 百分比） |
| `status` | 横幅，黄色 dim |
| `compact` | 横幅 `── context compacted ──`，dim |
| `subagents` | 每个 child 一行 `agent: <desc> 启动`，dim |
| `subagent_state` | 一行 `agent: <desc> 退出 (state)`，dim |
| `plan_exit_request` | 交互提示（见第五节） |
| `permission_request` | 交互提示（见第五节） |
| `subagent_permission_request` | 交互提示（见第五节） |
| `done` | 不渲染，仅结束 RUNNING 回到 IDLE |
| `cancelled` | 一行 dim `── cancelled ──` |
| `interrupted` | 一行 dim `── interrupted ──` |
| `error` | 一行红色 `✗ <text>` |

### 行状态管理

流式事件（`thinking` / `text` / `tool_output`）按增量追加，可能不含行尾换行。渲染器维护一个「是否处于行首」标记：

- 追加增量时直接写，更新标记（依增量是否以 `\n` 结尾）。
- 遇到块式事件（`tool_start` / `tool_result` / `subagents` / `done` 等）时，若不在行首，先补一个 `\n`，保证块式内容独占行。
- **思维链与正文分行**：渲染器记录上一段流式类型；`text` 到来时若上一段是 `thinking` 且不在行首，先补 `\n`，使正文另起一行（思维链已自带行尾换行则不重复补）。

### 工具调用格式（`tool_start`）

橘色（`#ce9178`，与 web 端 `ToolEntry` 标签同色）的 `[<tool>: <args>]`，格式与 web 端 `label` 一致：

| 工具 | 格式 |
|---|---|
| `bash` | `[bash: <command>]` |
| `read` | `[read: <filename>]` |
| `edit` | `[edit: <filename>]` |
| `write` | `[write: <filename>]` |
| `glob` | `[glob: <pattern>]`（有 `path` 时附 ` in <path>`） |
| `grep` | `[grep: <pattern>]`（有 `glob`/`path` 时附 ` (<glob>)` / ` in <path>`） |
| `TodoList` | `[TodoList]` |
| `task` | `[task: <n> subagent(s)]` |
| `exit_plan_mode` | `[exit_plan_mode]` |

### 工具结果渲染（`tool_result`）

| 工具 | 渲染 |
|---|---|
| `edit` | **inline diff**（`diff` 字段），跳过 `result` 摘要文本 |
| `bash` | 跳过正文（`tool_output` 已实时流式输出过）；若 `result` 含 `[exit code: N]` 且 N≠0，补一行红色 exit code |
| `task` | 跳过（subagent 启动/退出已由 `subagents` / `subagent_state` 事件展示，聚合 `<task_result>` 结论不打印） |
| `read` / `grep` / `glob` / `write` / `TodoList` | 原样输出 `result` 文本，白色 |
| 其他 | 原样输出 `result` 文本 |

### Inline diff

复用 web 端 inline（unified 单列）版本的 diff 行结构。后端 `tool_result.diff` 是 `[{type, old, new, text}]`，`type ∈ {equal, added, removed}`。CLI 逐行渲染：

| type | 前缀 | 行号 | 颜色 |
|---|---|---|---|
| `equal` | ` ` | `new`（或 `old`） | dim 灰 |
| `added` | `+` | `new` | 绿色 |
| `removed` | `-` | `old` | 红色 |

每行格式：`<行号> <前缀> <text>`。无需任何配对 / 重组逻辑（这正是 inline 比 split 简单之处）。

## 五、交互式提示（权限 / plan 退出）

三种事件需要用户当场回答：

| 事件 | 提示 | 解析 |
|---|---|---|
| `permission_request` | `path`、`tool`、`message` | `agent.resolve_permission(approved)` |
| `plan_exit_request` | （请求退出 plan 模式） | `agent.resolve_plan_exit(approved)` |
| `subagent_permission_request` | `child_description`、`path`、`tool` | `agent.resolve_permission(approved)`（权限落在父 agent 上） |

### 提示的串行性

这些事件发生时 agent 正阻塞等待决议（`_permission_event.wait()` / `_plan_exit_event.wait()`），期间不会有其他输出流，提示能干净地显示。此时 RUNNING 消费者暂停在事件循环上，直接用 `input()` 同步读 y/n -- 无并发输出竞争，天然串行。

权限提示的 `input()` 期间 RUNNING 的 `loop.add_signal_handler(SIGINT, agent.cancel)` 仍挂着，但该 handler 在 `input()` 阻塞事件循环时无法执行（Ctrl+C 会被吞）。故提示前临时 `remove_signal_handler(SIGINT)` 切回 Python 默认 handler（Ctrl+C 抛 `KeyboardInterrupt` -> `agent.cancel()`），提示结束后再 `add_signal_handler` 装回（见第三节）。

提示格式（黄色）：

```
⚠ <message>
   path: <path>   tool: <tool>
允许? [y/N] 
```

默认 N（拒绝）。`y` / `yes` 批准。批准后路径加入 `additional_dirs` 并持久化（与 web 端一致）。

## 六、Subagent 展示

CLI 主界面**只订阅主 agent 的 broker**。子 agent 跑在自己的 broker 上，其内部 thinking / 工具调用**不会**推到主界面（与 web 端一致：主界面只通过 `subagents` / `subagent_state` 两个事件感知子 agent）。

因此 CLI 对 subagent 的展示就是两行式：

```
agent: <description> 启动        ← subagents 事件（每个 child 一行）
agent: <description> 退出 (state) ← subagent_state 事件
```

`subagents` 事件的 children 带 `description`，`subagent_state` 事件只带 `session`，渲染器维护 `session -> description` 映射（从 `subagents` 事件建立）以补全退出行的描述。

- Ctrl+C（`cancel()`）会取消主 agent 并连带取消所有 subagent（硬停，无总结），与 web 端主 session stop 按钮一致。
- 不支持切到子 session 查看、不支持单独中断某个 subagent（v1 non-goal）。

## 七、会话与持久化

- `-t` 新建 session；`-c` 续接当前目录最新的 session（无则新建）。session 目录在 `~/.config/trilobite/sessions/<uuid>/`。
- `history.json`、`session.json`、`agent.log` 照常写入（与 web 模式完全一致），意味着 CLI 跑出的 session 之后能在 web 端侧边栏看到、点进去查看历史。
- `session.json` 不额外维护时间字段；「最新 session」按 `history.json` 的 mtime（最后一次存盘时间）排序，回退 `created_at`，web 与 CLI 的所有活动都自动反映。续接时 agent 加载 `history.json` 保持上下文，但终端不回显历史。

## 八、实现位置

| 文件 | 改动 |
|---|---|
| `src/trilobite/cli.py` | **新增**。CLI 入口 `run_cli(working_dir, resume)`、session 查找/创建、REPL 主循环、`Renderer`、配色常量、工具调用/diff 格式化、交互提示。全部 async，跑在 `asyncio.run` 上。 |
| `src/trilobite/server.py` | `main()` 用 `argparse`：`-s`（默认）-> `uvicorn.run`；`-t` -> `run_cli(..., resume=False)`；`-c` -> `run_cli(None, resume=True)`。 |

不改动 `agent.py` / `broker.py` / 工具层--CLI 是纯新增的订阅者 + 渲染器。唯一例外：给 `Agent` 加一个极小的 `async def aclose()`（关闭其 httpx client），CLI 退出时调用；web 模式 agent 常驻进程、无需调用，行为不变。

## 九、设计决策

1. **steering**：v1 放弃。改用 readline `input()` 后无法在运行中多路复用 stdin（bash 语义：前台命令运行时不读输入），换取退格/方向键正常工作。运行中要中断用 Ctrl+C。
2. **token 用量**：每个 `usage` 事件打一行 dim `Tokens: <N> / <max> (<pct>%)`，与 web 端 TokenBar 同格式。
3. **会话续接**：`-c` 续接当前目录最新的 session（按 `history.json` 的 mtime 排序），agent 加载历史保持上下文；终端不回显历史（只显示 `resumed` 提示）。无历史 session 时退化为新建。
4. **plan/build 模式**：v1 固定 build 模式，不支持运行中切换。
5. **Ctrl+C**：RUNNING 时 = `cancel()` 回 IDLE（由 `loop.add_signal_handler` 回调）；IDLE 时 = 退出（`input()` 抛 `KeyboardInterrupt`）。两者都走 `cancel()`，取消主 + 所有 subagent。
