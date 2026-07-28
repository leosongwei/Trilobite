# CLI 模式（命令行交互）

> 状态：**设计稿，待审核**。审核通过后再实现。本文是 CLI 模式的产品规格。

## 一、概述

Trilobite 目前只有 web 模式：启动 uvicorn 服务，前端 Vue 应用通过 SSE 订阅 agent 输出。本功能增加一个**不启动服务器**的命令行交互模式，用 `-c` 选项触发，以**命令执行目录**作为 agent 的 working dir，提供一个**类 bash** 的行式 REPL 界面（不是 opencode 那种 ncurses 全屏 TUI）。

核心价值：在终端里快速起一个 coding agent 会话，无需开浏览器、无需占端口；适合 SSH 远程、无头环境、脚本化场景。

### 设计原则

- **复用，不重写**：CLI 不另起一套 agent 逻辑，而是直接实例化 `Agent`，像 SSE 端点一样 `attach_subscriber()` 订阅 `StreamBroker` 队列，把事件渲染到终端。agent / broker / 工具 / 权限 / 压缩 / subagent 全部原样复用，CLI 只是一个新的「订阅者 + 渲染器」。
- **类 bash，非 TUI**：行式输入输出，不接管全屏、不画边框面板、不做 ncurses。输出和输入在同一行流上自然流动，能 pipe、能滚动。
- **颜色区分语义**：用 ANSI 颜色区分不同信息源，不靠框线布局。

### v1 范围（本次实现）

- ✅ `trilobite -c [working_dir]` 启动 CLI；不带 `-c` 时行为不变（启动 web 服务器）。
- ✅ 以 cwd（或指定目录）为 working dir，每次启动新建一个 session。
- ✅ 类 bash REPL：输入消息 -> agent 运行 -> 流式输出 -> 输入下一条。
- ✅ 彩色输出（见第四节）。
- ✅ inline diff 渲染（复用 web 端 inline 版本的 diff 行结构）。
- ✅ `/stop` 指令中断当前 run（主 session + 所有 subagent，与 web 端 stop 按钮一致）。
- ✅ 运行中可输入消息 steering（中途引导）。
- ✅ 权限请求 / plan 退出请求：交互式 y/n 提示。
- ✅ `/exit`、`/quit`、Ctrl+D 退出。

### v1 不做的事（non-goals）

- ❌ **Markdown 解析**：终端原样输出纯文本，不做 Markdown / 代码块 / 公式渲染。
- ❌ **subagent 交互**：不切换 / 查看子 session、不单独中断某个 subagent。subagent 自然跑完，或由 `/stop` 连带取消（与 web 端主 session stop 行为一致）。subagent 的内部 thinking / 工具调用**不**在 CLI 主界面展示，只展示「启动 / 退出」两行（见第六节）。
- ❌ **会话恢复**：每次 `-c` 新建 session，不恢复历史 session（历史仍持久化到磁盘，只是 CLI 不读回）。
- ❌ **ncurses / 全屏 TUI**：不接管终端、不做分屏面板。
- ❌ **plan/build 模式切换**（Tab）：v1 不支持运行中切换模式（沿用 session 创建时的 build 模式）。

## 二、入口与启动

### 命令行

```
trilobite                # 启动 web 服务器（原有行为，不变）
trilobite -c             # CLI 模式，working dir = cwd
trilobite -c <dir>       # CLI 模式，working dir = <dir>
```

`server.py:main()` 改为用 `argparse` 解析参数：

| 参数 | 说明 |
|---|---|
| `-c`, `--cli` | 进入 CLI 模式 |
| `[working_dir]`（位置参数，可选） | CLI 模式的 working dir，默认 `os.getcwd()` |

不带 `-c` 时走原有 `uvicorn.run(...)` 路径，**完全向后兼容**。

### 启动流程（CLI 模式）

1. `init_config()` 加载配置（与 web 模式共用 `~/.config/trilobite/config.yaml`）。
2. 在 `get_sessions_dir()` 下新建一个 session 目录（`uuid4().hex`），写 `session.json`（`working_dir`、`plan_mode: false`、`additional_dirs: []`）。
3. 实例化 `Agent`（与 `server.py` 创建 session 时完全相同的参数：`name`、`working_dir`、`session_dir`、`config`、`registry`）。
4. `await agent.attach_subscriber()` 拿到事件队列 + `init` 快照（新 session 历史为空）。
5. 进入 REPL 主循环（见第三节）。

> session 的创建逻辑与 `POST /api/sessions` 端点一致，CLI 只是把「HTTP 请求驱动」换成「stdin 驱动」。

## 三、交互模型：状态机

CLI 是一个两状态循环：

```
        ┌──────────────────────────────────┐
        │  IDLE（空闲，等待输入）            │
        │  显示蓝色 ❯ 提示，读取一行         │
        └──────────────────────────────────┘
          │ 输入消息（非命令）
          │ start(agent) ───────────────────────┐
          ▼                                      ▼
        ┌──────────────────────────────────┐
        │  RUNNING（运行中）                 │
        │  事件消费者：渲染 broker 事件       │
        │  输入读取器：并发轮询 stdin         │
        │    /stop  -> cancel                │
        │    /exit  -> cancel + 退出         │
        │    其他    -> steer                │
        └──────────────────────────────────┘
          │ 收到 done/cancelled/error/interrupted
          └──────────────────────────────────┘
          ▼
        回到 IDLE
```

### IDLE 状态

显示蓝色 `❯ ` 提示，读取一行输入：

| 输入 | 行为 |
|---|---|
| 空行 | 重新提示（忽略） |
| `/stop` | 未运行，打印 dim `（未运行）` |
| `/exit`、`/quit` | 退出 CLI |
| Ctrl+D（EOF） | 退出 CLI |
| 其他非空 | `await agent.start(text)`，进入 RUNNING |

**人类输入蓝色**：IDLE 时无并发输出，输入回车后用 ANSI 把该行重渲染为蓝色（`input()` 先以默认色回显，回车后 `\033[A\033[2K` 上移清行，重写蓝色 `❯ <text>`）。仅 IDLE 做此重渲染（无并发，安全）。

### RUNNING 状态

进入 RUNNING 后，两个协程并发：

1. **事件消费者**：从 broker 队列取事件并渲染（见第四节）。遇到 `permission_request` / `plan_exit_request` / `subagent_permission_request` 时，把问题投递给输入读取器统一处理（见第五节）。遇到 `done` / `cancelled` / `error` / `interrupted` 时结束本 run，回到 IDLE。
2. **输入读取器**：用 `select` 以 0.2s 超时轮询 stdin（这样能在 run 结束时及时退出，不会被阻塞的 `input()` 卡住）。读取到的行：

   | 输入 | 行为 |
   |---|---|
   | `/stop` | `agent.cancel()`（取消主 + 所有 subagent） |
   | `/exit`、`/quit` | `agent.cancel()` + 标记退出 |
   | Ctrl+D | 同 `/exit` |
   | 其他非空 | `await agent.steer(text)`（中途引导） |

   RUNNING 期间**不显示提示符**（类 bash：命令运行时不显示新提示）。用户直接输入，终端默认回显。steering 输入以默认色回显即可（不做蓝色重渲染，避免与并发输出冲突）。

> **为什么用 select 轮询而非 `input()`**：`/stop` 需要在 run 进行中输入，而 `input()` 会阻塞线程且无法在 run 结束时取消。`select(stdin, timeout=0.2)` 每 0.2s 醒来一次检查 run 是否结束，既支持中途输入，又能在 `done` 后及时返回 IDLE。

### 退出

`/exit`、`/quit` 或 Ctrl+D：若正在运行先 `agent.cancel()`，等待 run 收尾，然后退出进程。不做额外清理（session 历史已落盘）。

## 四、事件渲染与配色

CLI 订阅 broker 队列，对每个事件按下表渲染。颜色仅在 stdout 是 tty 时启用（`sys.stdout.isatty()`），否则纯文本。

### 配色总表

| 信息源 | 颜色 | 说明 |
|---|---|---|
| 人类输入 | **蓝色** | IDLE 提示符 `❯ ` 与输入文本 |
| 思维链（thinking） | **绿色** | reasoning 增量 |
| LLM 正文（text） | **白色** | 模型回复正文增量 |
| 工具调用 | **白色** | `tool_start` 一行 |
| 工具输出 | **白色** | bash 实时输出 / 工具结果 |
| diff 新增行 | **绿色** `+` | inline diff |
| diff 删除行 | **红色** `-` | inline diff |
| diff 上下文 | **dim 灰** | inline diff 未改动行 |
| subagent 启动/退出 | **dim 灰** | 多行 |
| 错误 | **红色** | error 事件 |
| 状态/压缩 | **黄色 dim** | status / compact 横幅 |
| token 用量 | **dim 灰** | usage 事件，一行 |

> 「白色」即终端默认前景色（不额外着色或显式 `\033[37m`），保证在浅色 / 深色终端上都可读。

### 逐事件渲染

| 事件 | 渲染 |
|---|---|
| `init` | 跳过（新 session 历史为空） |
| `user` | 跳过（人类输入已由输入侧显示，避免重复） |
| `turn` | 跳过 |
| `thinking` | 流式原样追加，绿色 |
| `text` | 流式原样追加，白色 |
| `tool_stream` | 跳过（工具调用在 `tool_start` 时一次性显示完整参数，不逐字符流式） |
| `tool_start` | 一行：`❯ <tool call>`（`❯` 前缀 dim，调用串白色）。见下「工具调用格式」 |
| `tool_output` | 逐行流式追加（bash stdout/stderr），白色，每行末补 `\n` |
| `tool_result` | 见下「工具结果渲染」 |
| `usage` | 一行 dim：`· <N> / <max> tokens` |
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

### 工具调用格式（`tool_start`）

`❯ ` + 简明调用串，按工具类型提取最相关参数：

| 工具 | 格式 |
|---|---|
| `bash` | `bash$ <command>` |
| `read` | `read <filename>`（有 `start_line`/`limit_*` 时附括号说明） |
| `edit` | `edit <filename>` |
| `write` | `write <filename> [mode]` |
| `glob` | `glob <pattern>` |
| `grep` | `grep <pattern>` |
| `TodoList` | `TodoList` |
| `task` | `task (<n> subagents)` |
| `exit_plan_mode` | `exit_plan_mode` |

### 工具结果渲染（`tool_result`）

| 工具 | 渲染 |
|---|---|
| `edit` | **inline diff**（`diff` 字段），跳过 `result` 摘要文本 |
| `bash` | 跳过正文（`tool_output` 已实时流式输出过）；若 `result` 含 `[exit code: N]` 且 N≠0，补一行红色 exit code |
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

### 串行化 stdin

RUNNING 期间事件消费者与输入读取器并发，但**只有输入读取器拥有 stdin**。事件消费者遇到需交互的事件时，把「问题」投递到一个 `asyncio.Queue`，输入读取器在下一轮轮询前优先取出并提问（打印提示 + 读 y/n + 解析）。这样避免两个协程争抢 stdin。

由于这些事件发生时 agent 正阻塞等待决议（`_permission_event.wait()` / `_plan_exit_event.wait()`），期间不会有其他输出流，提示能干净地显示。输入读取器的 0.2s 轮询保证问题在 0.2s 内被取出。

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

- `/stop` 会 `cancel()` 主 agent 并连带取消所有 subagent（硬停，无总结），与 web 端主 session stop 按钮一致。
- 不支持切到子 session 查看、不支持单独中断某个 subagent（v1 non-goal）。

## 七、会话与持久化

- 每次 `-c` 新建 session（`~/.config/trilobite/sessions/<uuid>/`）。
- `history.json`、`session.json`、`agent.log` 照常写入（与 web 模式完全一致），意味着 CLI 跑出的 session 之后能在 web 端侧边栏看到、点进去查看历史。
- CLI 不读取 / 恢复已有 session（v1 non-goal）。

## 八、实现位置

| 文件 | 改动 |
|---|---|
| `src/trilobite/cli.py` | **新增**。CLI 入口 `run_cli(working_dir)`、REPL 主循环、`Renderer`、配色常量、工具调用/diff 格式化、交互提示。全部 async，跑在 `asyncio.run` 上。 |
| `src/trilobite/server.py` | `main()` 改为 `argparse`：`-c` -> `asyncio.run(run_cli(...))`；否则原 `uvicorn.run(...)`。 |

不改动 `agent.py` / `broker.py` / 工具层——CLI 是纯新增的订阅者 + 渲染器。

## 九、待确认问题

1. **steering 输入颜色**：IDLE 输入蓝色（重渲染），但 RUNNING 期间的 steering 输入因并发输出不做重渲染，呈默认色。是否接受此不一致？还是 RUNNING 期间干脆不支持 steering（更纯粹的 bash 体验，只保留 `/stop`）？
2. **token 用量**：是否每个 turn 都打一行 dim 用量？还是只在超过阈值（如 70%）时提示？还是完全不显示？
3. **会话恢复**：v1 不恢复，但是否需要一个 `-c --resume <session>` 或列出最近 session 选择的入口？（建议 v1 不做，留作后续。）
4. **plan/build 模式**：v1 固定 build 模式。是否需要 CLI 下也支持 Tab 切换或 `/plan`、`/build` 指令？（建议 v1 不做。）
5. **`/stop` vs Ctrl+C**：`/stop` 走 `cancel()`（与 web stop 一致）。Ctrl+C（SIGINT）是否也绑定为同样的 cancel？还是 Ctrl+C 直接强退进程？（建议：Ctrl+C = cancel 当前 run 后回 IDLE，连按两次退出；与 `/stop` 等价。）
