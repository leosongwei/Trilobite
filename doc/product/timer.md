# Timer（sleep_until 定时挂起）

> 状态：**已实现**。本文是 timer 功能的产品规格。

## 一、概述

`sleep_until` 是主 agent 的一个**虚拟工具**（仅 LLM 可见定义，执行逻辑在 `Agent` 中，与 `exit_plan_mode`/`task` 同路径）：接受一个目标时间，把**当前会话挂起**（suspend）到该时刻，到点后自动唤醒并继续对话。

- **同会话续跑**：挂起不派生新 agent、不新建 session。模型调用 `sleep_until` 后本轮流结束；到点唤醒时追加一条 `⏰ 定时唤醒（<当前时间>）` 合成 user 消息并启动新一轮 run，模型在**同一上下文**里继续之前的工作。模型醒来看到的信息 = 睡前的工具结果占位文本 + 带当前时间的唤醒消息。
- **挂起零开销**：挂起期间无 LLM 请求、不烧 token；run 结束、broker 置为非运行态。
- **跨重启存活**：挂起状态持久化在 `session.json` 的 `sleep_until` 字段（epoch 秒），服务重启后由 TimerService 重载；停机期间错过的唤醒点在启动后立即补触发（迟到唤醒）。服务停机期间什么都不发生（无运行、无消耗）；**过期的挂起一律补触发**——迟到超过 60 秒的唤醒消息显式标注原定时间与迟到时长（`⏰ 定时唤醒（<now>，原定 <target>，迟到了 <duration>）`），模型据此自行判断任务是否还有意义（继续做 / 一句话收尾）。
- **蓝点标识**：挂起中的会话在侧边栏显示蓝点（`has_sleep`），排序置顶（运行中 > 挂起 > 普通）；用户发消息可提前唤醒。

典型用例：等 CI/构建跑完再检查（`+10m`）、明天早上九点继续今天的任务（`2025-06-02 09:00`）、提醒用户到点做某事、轮询式任务（醒来检查、再睡）。

### 与旧定时 subagent 的关系

本功能取代已移除的定时 subagent（cron），旧规格存档于 `doc/product/archived/scheduled_subagent.md`。旧功能遗留的 `schedules.json` 与 `kind: "scheduled"` session 不再被任何代码读取；旧定时 session 在侧边栏中退化为挂在父 session 下的普通历史会话，可查看、可手动续聊、不再自动触发。

## 二、`sleep_until` 工具

### 参数

```jsonc
{
  "until": "2025-06-01 09:00"   // 必填，目标时间（本地时区），见下方格式
}
```

`until` 只接受两种格式（解析失败/时间非法时错误信息附带**当前本地时间**，模型可自行修正后重试）：

| 格式 | 语义 | 示例 |
|---|---|---|
| `+[n][s\|m\|h\|d]` | 相对时长（单位必填） | `+30m`、`+2h`、`+1d`、`+90s` |
| `YYYY-MM-DD HH:MM` | 绝对本地时间 | `2025-06-01 09:00` |

校验规则：

- 目标时间必须**严格在未来**且距现在 ≥ **5 秒**（过近视为无意义，报错）。
- 距现在最多 **365 天**（超出报错）。
- 相对时长（`+…`）的语义：模型往往不知道当前时间，这是首选表达方式；错误信息始终携带当前本地时间供绝对时间修正。

### 成功路径（模型视角）

1. 工具返回结果（同时作为 ToolResult 落历史）：

   > Sleeping until 2025-06-01 09:00 (8h from now). This run is suspended -- no tokens are spent while you sleep. The session wakes automatically at that time with a ⏰ wake-up message containing the current time, and you resume this conversation where you left off. If the user sends a message in the meantime, you resume early.

2. 同一条 model 消息中的其他工具调用照常执行完毕（挂起在整轮工具执行结束后生效），随后本 run 结束（发出 `done` 终态事件）。
3. 到点唤醒：历史末尾追加 `⏰ 定时唤醒（<当前本地时间>）` user 消息并启动新 run。模型看到睡前占位结果 + 唤醒消息，继续工作。

### 错误路径

- 解析失败 / 时间在过去 / 距今 <5s / 超过 365 天 / 缺参数：返回 `Error: …` 文本结果（含当前本地时间），不挂起，模型可立即重试。
- 服务未注入 TimerService（CLI 模式）：返回 `Error: sleep_until is only available for server sessions.`

## 三、挂起与唤醒机制

### 挂起（Agent 层）

`sleep_until` 在 Agent 的工具分发分支中执行（虚拟工具）：

1. 解析校验 `until`，得到 `wake_at`（epoch 秒）。
2. 调 `TimerService.register(session_name, wake_at)`：写入 `session.json` 的 `sleep_until` 字段（读-改-写，保留其他字段），并登记内存 pending 表。
3. 置 `agent._sleeping_until = wake_at`，发 `sleep_start` SSE 事件 `{session, until}`。
4. 返回占位结果文本（走通用路径插入 ToolResults、发 `tool_result` 事件）——**历史无悬空 tool call**，页面刷新/重建历史都完整。
5. 本轮消息内的其余工具调用继续执行；run 循环顶部检测到 `_sleeping_until` 即 break，run 正常收尾（`done` 事件、`broker.set_running(False)`）。`_pending_tool_results` 标志**保留**（不清除），它是唤醒后继续循环的驱动之一。

每 session 同时至多一个挂起；同一轮里第二次调用 `sleep_until` 时后一次覆盖前一次（重新 register）。

### 唤醒（TimerService 层）

新模块 `src/trilobite/timer.py`，全局单例，server 持有（与被移除的 CronService 同位置注入）：

- `__init__(sessions_dir, get_agent)`：`get_agent` 是 server 的 `_get_or_create_agent` 工厂（按需从磁盘恢复 agent 实例）。
- `load_all()`：启动时扫描 `sessions/*/session.json`，凡带 `sleep_until` 字段即登记 pending（**过期目标也登记**，tick 后立即触发迟到唤醒）。
- `register(name, wake_at)` / `cancel(name)` / `remove_session(name)` / `is_sleeping(name)` / `sleep_until(name)`：增删查；`cancel` 同步清除 session.json 字段，不在 pending 表时为无文件写的空操作。
- `wake(name) -> bool`：手动立即唤醒（供 REST 端点），未挂起返回 False。
- **tick 循环**：每秒一次，遍历 pending 表，`time.time() >= wake_at` 即触发 `_do_wake`（墙钟比较，系统时间跳变语义正确；单用户本地应用轮询开销可忽略）。
- `_do_wake(name)`：
  1. 若 agent 正在运行（挂起登记后同轮兄弟工具仍在跑）——**重新挂回 pending**（文件字段保留，中途崩溃仍可恢复），下一秒重试，唤醒不丢失。
  2. 清除 pending 与 `session.json` 字段，发 `sleep_end` 事件。
  3. `await agent.start(wake_message(wake_at))`：追加合成 user 消息（发 `user` 事件；迟到超 60 秒带"原定/迟到"标注）→ `set_running(True)` → 独立 asyncio task 跑 `run()`。run 循环因 `_pending_tool_results`（睡前的占位结果）+ 未读 user 消息（唤醒消息）继续运转。`start()` 的同步前缀（追加消息 + 置 running）先于其首个 await 执行，与 `/message` 同构的无竞态形状，用户消息不会与唤醒交叠出双 run。

### 各入口与挂起的交互

任何新 run（唤醒或用户消息）的 `run()` 序言统一结束挂起：清除 `_sleeping_until`、`TimerService.cancel`（幂等）、发 `sleep_end` 事件。序言同时检查内存标志与 TimerService 的 pending 表（`is_sleeping`）——重启后从磁盘恢复的实例没有内存标志，只查标志会漏掉提前发消息的取消，导致用户消息唤醒后定时器又多发一次唤醒。挂起轮的兄弟工具仍在执行时收到 steer，run 循环顶部检测到未读 user 消息同样立即结束挂起并继续循环——**任何用户输入（消息或 steer）都是提前唤醒**，模型当轮即响应，不会等到目标时刻。

| 入口 | 行为 |
|---|---|
| 用户发消息（挂起中，idle） | 正常走 `start()` → 新 run 序言清除挂起，模型看到睡前的占位结果 + 用户新消息，**提前唤醒**；蓝点消失，定时唤醒不再触发 |
| 用户 steer（挂起轮兄弟工具执行中） | run 循环顶部清除挂起并继续循环，模型当轮响应用户消息；若模型再次调用 `sleep_until` 则重新挂起 |
| `POST /api/sessions/{name}/wake` | 手动立即唤醒（`_do_wake` 路径）；未挂起 409，agent 运行中 409（唤醒已挂起、run 结束后自动触发，发消息可立即 steer） |
| `/compact` | 挂起中发送即提前唤醒（普通消息路径）；若合并后的 turn 正是压缩轮，压缩在清除挂起后照常进行 |
| `/revert` | 回滚历史的同时取消挂起（清除 `sleep_until`），避免唤醒消息落到缺失上下文里 |
| 删除 session | `TimerService.remove_session`（丢弃 pending），目录随级联删除 |
| 模式切换（Tab / `/mode`） | 允许；`session.json` 读-改-写保留 `sleep_until` 字段，唤醒时按新模式运行（`<modeswitch>` 通知随唤醒 run 注入） |
| interrupt / cancel | 挂起中无运行 task，自然不适用；UI 停止按钮在非流式态不可用。挂起轮兄弟工具执行中被中断：挂起仍生效，到点照常唤醒 |
| 服务重启 | `load_all` 重载 pending（含过期目标）；停机期间到点的目标在启动后 1 秒内补触发，迟到超 60 秒的唤醒消息带"原定/迟到"标注。重启后用户在补触发前发消息：run 序言经 `is_sleeping` 检查取消挂起，定时唤醒不再触发 |

### CLI 不支持

CLI 的 IDLE 循环阻塞在同步 `input()`（事件循环停摆），tick 无法及时触发。CLI 模式创建的 Agent 不注入 TimerService，`sleep_until` 调用返回错误文本。

## 四、蓝点与前端

- **数据源**：`GET /api/sessions`（3s 轮询）对每个主 session 附加 `has_sleep`（`session.json` 的 `sleep_until` 字段存在即 true）与 `sleep_until`（epoch 秒，tooltip 显示"sleeping until <时间>"）。
- **状态点优先级**（`frontend/src/utils/sessionStatus.ts`）：运行（绿点闪烁）> 挂起（蓝点 `pending`）> 空闲（灰点）；project 行状态点由成员聚合。
- **排序**：运行中 > 挂起 > 最近活动 > 名字（rank 逻辑在 `SessionSidebar.vue` 的 `sortTop`，以 `has_sleep` 计 rank）。
- **SSE 事件**（进本 session 的 broker，不落历史）：
  - `sleep_start {session, until}`：挂起生效，前端即时置 `sleep_until`（蓝点不等 3s 轮询）。
  - `sleep_end {session}`：唤醒，前端即时清除。
- **唤醒消息渲染**：`⏰` 前缀的 user 消息（直播与历史重建两条路径）渲染为"定时唤醒"分隔线（divider，`run-divider` 样式）；历史里每次挂起-唤醒形成天然的对话分段。
- **挂起横幅**：当前查看的会话处于挂起时，顶部显示 `⏳ 挂起至 <时间>` 横幅 + **立即唤醒**按钮（调 `/wake`）；输入框保持可用（发消息即提前唤醒，语义一致）。
- **工具条目**：`sleep_until` 的 ToolEntry 按通用工具渲染（名称 + 参数 + 占位结果文本），无需特化。

## 五、持久化

- `session.json` 字段 `sleep_until: <epoch 秒>`（挂起中存在，唤醒/取消时删除）。无独立数据文件。
- 睡前占位 ToolResult 与唤醒 user 消息都走既有 `history.json` 持久化路径，刷新/重启后历史完整。
- 唤醒不重建 system prompt、不插入 CompactMarker：挂起只是"时间上的暂停"，上下文原样保留（token 用量照常累计，超阈值时唤醒后的 run 走正常自动压缩）。

## 六、权限与安全

- **仅主 agent 可用**：`sleep_until` 由 `BuildModePermission` 与 `PlanModePermission` 共同暴露与放行（工具无副作用，plan 模式只读语义不受影响；两模式仍暴露同一全量工具前缀，缓存稳定）；subagent 角色（explore/general）工具集不含它，`intercept` 兜底拦截——有界任务不允许睡眠（父 agent 会挂起等待）。
- **无资源消耗**：挂起仅是一个时间戳 + 每秒一次的字典扫描；不占用 LLM 连接、线程或进程。
- 上限：单次 ≤365 天、≥5 秒；每 session 至多一个挂起（后设覆盖）。
- 挂起期间 `/compact` 提前唤醒（普通消息路径）、`/revert` 取消挂起、删除 session 级联清理，防止悬挂的 pending 唤醒到已变更的上下文。

## 七、Non-goals

- ❌ 周期性调度（cron 语义）：需要重复行为时，模型醒来后再次调用 `sleep_until` 即可（每次睡眠都是一次显式决策，附上下文）。
- ❌ subagent / CLI 睡眠（见上文）。
- ❌ 唤醒结果通知其他 session：唤醒只发生在本 session（不存在 fire-and-forget 的跨 session 语义）。
- ❌ 修改挂起时间：要调整就唤醒后重新睡（`/wake` 或发消息）。

## 八、实现拆解

1. **`src/trilobite/timer.py`**（新模块）：`TimerService`——pending 表 + tick 循环 + `load_all`/`register`/`cancel`/`remove_session`/`is_sleeping`/`sleep_until`/`wake`/`_do_wake`；`parse_sleep_until` 时间解析、`wake_message` 唤醒消息、`sleep_placeholder` 占位结果文本；`session.json` 的 `sleep_until` 字段读-改-写。
2. **`src/trilobite/tool_call.py`**：删 `CRON_CREATE_DEF`/`CRON_LIST_DEF`/`CRON_DELETE_DEF`/`CRON_TOOL_DEFS`；增 `SLEEP_UNTIL_DEF`（虚拟工具定义，描述含时间格式与相对时长建议）。
3. **`src/trilobite/permission.py`**：删 `CRON_TOOL_NAMES`、`exposes_cron`、`CronSubagentPermission`；主模式（build/plan）允许并放行 `sleep_until`（加入两者 `tool_names`；plan 模式的 `tool_names` 增补 `sleep_until`）。
4. **`src/trilobite/agent.py`**：删 scheduled/cron 全部代码（构造参数、`CronBoundaryError`、分发分支、`_run_cron_tool`、`start_scheduled_fire`、`is_scheduled`、`kind` 的 scheduled 值等）；增 `timer_service` 构造参数、`_sleeping_until` 状态、`_run_sleep_tool` 分发分支、run 循环顶部的挂起 break、run 启动时的挂起取消。
5. **`src/trilobite/prompts.py`**：删 `SYSTEM_PROMPT` 的 cron 段与 `CRON_ROLE_PROMPT`；增 timer 段（用法、格式、睡前收尾建议）。
6. **`src/trilobite/server.py`**：删 CronService 装配/端点/`_scheduled_info`/scheduled 分支；增 TimerService 装配（startup `load_all`+`start`，shutdown）与注入、`GET /api/sessions` 的 `has_sleep`/`sleep_until`、`POST /api/sessions/{name}/wake`、`/revert` 取消挂起、删除 session 级联。
7. **`src/trilobite/cli.py`**：不注入 TimerService（`sleep_until` 报错路径）。
8. **前端**：`types.ts`（SSEEvent 增 `sleep_start`/`sleep_end` 删 cron 三事件；Session 删 schedule 族字段增 `has_sleep`/`sleep_until`）、`sessionStatus.ts`（蓝点语义）、`store.ts`（事件处理、`⏰` divider、历史重建）、`SessionSidebar.vue`（删 scheduled 徽标/状态点/tooltip，排序字段）、`App.vue`（删 scheduled 面板，增挂起横幅 + 立即唤醒）、`ChatInput.vue`（删 scheduled 只读分支）、`api.ts`（删 `deleteSchedule`，增 `wakeSession`）。
9. **`pyproject.toml`**：删 `croniter` 依赖；版本号 bump。
