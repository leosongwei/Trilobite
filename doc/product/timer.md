# Timer（sleep_until 定时挂起）

> 状态：**已实现**。本文是 timer 功能的产品规格。

## 一、概述

`sleep_until` 是主 agent 的一个**虚拟工具**（仅 LLM 可见定义，执行逻辑在 `Agent` 中，与 `exit_plan_mode`/`task` 同路径）：接受一个目标时间，把**当前会话挂起**（suspend）到该时刻，到点后自动唤醒并继续对话。

- **模型视角：一个执行得特别慢的普通工具**。调用 `sleep_until` 不产生工具结果；结果在会话唤醒时才构造并插入历史——文案写明唤醒情况（准点 / 提前 / 迟到 / 被打断）。唤醒后模型看到的输入就是这批工具结果，没有任何合成 user 消息。
- **同会话续跑**：挂起不派生新 agent、不新建 session。唤醒在同一上下文里继续之前的工作；上下文原样保留（token 用量照常累计，超阈值时唤醒后的 run 走正常自动压缩）。
- **挂起零开销**：挂起期间无 LLM 请求、不烧 token；run 正常收尾（`done` 事件、broker 置为非运行态），会话接受新输入。
- **跨重启存活**：挂起状态持久化在 `session.json` 的 `sleep_until` 字段（epoch 秒），服务重启后由 TimerService 重载；停机期间错过的唤醒点在启动后立即补触发，唤醒文案带"原定/迟到"标注。
- **蓝点标识**：挂起中的会话在侧边栏显示蓝点（`has_sleep`），排序置顶（运行中 > 挂起 > 普通）。

典型用例：等 CI/构建跑完再检查（`+10m`）、明天早上九点继续今天的任务（`2025-06-02 09:00`）、提醒用户到点做某事、轮询式任务（醒来检查、再睡）。

### 与旧定时 subagent 的关系

本功能取代已移除的定时 subagent（cron），旧规格存档于 `doc/product/archived/scheduled_subagent.md`。旧功能遗留的 `schedules.json` 与 `kind: "scheduled"` session 不再被任何代码读取。

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

- 目标时间必须**严格在未来**且距现在 ≥ **5 秒**（过近视为无意义，报错）。相对时长是首选表达——模型往往不知道当前时间。
- 距现在最多 **365 天**（超出报错）。

### 执行语义

1. **批内最后执行**：同一轮声明的其他工具调用先执行完（执行循环把 `sleep_until` 稳定重排到批尾），随后挂起开始。
2. **无结果返回**：调用成功即挂起（错误路径除外，见下）。工具结果延迟到唤醒时交付——挂起期间该调用在历史上处于未应答状态，前端渲染为"sleeping..."的 pending 工具条目。
3. **唤醒时交付结果**：延迟结果插入本批既有的 `ToolResults` 条目（`insert_result` 单一入口，`ModelMessage + ToolResults` 结构与"结果先于用户消息"不变量全程保持），并发 `tool_result` 事件。唤醒 run 的第一个请求里，模型一次性看到整批工具结果（兄弟调用结果 + sleep 结果）。

### 错误路径

- 解析失败 / 时间在过去 / 距今 <5s / 超过 365 天 / 缺参数：返回 `Error: …` 文本结果（含当前本地时间），不挂起，模型可立即重试。
- 服务未注入 TimerService（CLI 模式）：返回 `Error: sleep_until is only available for server sessions.`

## 三、挂起与唤醒机制

### 挂起（Agent 层）

`sleep_until` 在 Agent 的工具分发分支中执行（虚拟工具）：

1. 解析校验 `until`，得到 `wake_at`（epoch 秒）。
2. 调 `TimerService.register(session_name, wake_at)`：写入 `session.json` 的 `sleep_until` 字段（读-改-写，保留其他字段），并登记内存 pending 表。
3. 置 `agent._sleeping_until = wake_at`，发 `sleep_start` SSE 事件 `{session, until}`。
4. 向工具循环返回 sleeping 标记：该调用跳过结果事件与结果落盘（兄弟调用照常即时落盘）。
5. run 循环顶部检测到 `_sleeping_until` 即 break，run 正常收尾（`done` 事件）。`_pending_tool_results` 标志**保留**——它是唤醒后继续循环的驱动之一。

每 session 同时至多一个挂起；同一轮里第二次调用 `sleep_until` 重新 register（后设目标生效），先前的调用在唤醒时标注 superseded。

### 唤醒（TimerService 层）

`src/trilobite/timer.py` 的 `TimerService` 全局单例，server 持有：

- **tick 循环**：每秒一次，遍历 pending 表，`time.time() >= wake_at` 即触发 `_do_wake`（墙钟比较，系统时间跳变语义正确）。agent 仍在运行（挂起轮批尾尚在执行）时重新挂回 pending（文件字段保留），下一秒重试，唤醒不丢失。
- `load_all()`：启动时扫描 `sessions/*/session.json`，凡带 `sleep_until` 字段即登记 pending（**过期目标也登记**，tick 后立即触发迟到唤醒）。
- `wake(name)`：手动立即唤醒（`POST /wake`）；`abort(name)`：停止按钮对挂起的中断。
- `_do_wake(name, reason)`：清 pending 与 `session.json` 字段 → `agent.resume_from_sleep(wake_at, reason)`：交付延迟结果（插入 ToolResults + 发 `tool_result` 事件）→ 发 `sleep_end`（蓝点即时清除，覆盖从磁盘恢复的实例）→ `set_running(True)` → 启动唤醒 run。`set_running` 先于任何 await，与 `/message` 同构的无竞态形状。

### 唤醒文案（`sleep_result_text`）

延迟结果的文案由挂起结束的原因决定，全部携带真实当前时间，迟到超过 60 秒显式标注原定时间与迟到时长：

| 原因 | 触发入口 | 文案要点 |
|---|---|---|
| 准点 / 迟到 / 提前（按时钟判定） | 到点 tick、`POST /wake` | `Woke on schedule…` / `Woke at <now> -- <时长> late (the server was down or busy)` / `Woken early by the user` |
| 用户打断 | 挂起中收到用户消息 | `Interrupted by a user message…The user's message follows`（结果位于用户消息**之前**） |
| 中断挂起（停止按钮） | 挂起中 `POST /interrupt` | `sleep_until interrupted by the user…The suspension is over`（不启动唤醒 run，结果等下次 run 才被看到） |
| 批内取消 | 挂起轮批尾执行期间收到 steer | `sleep_until cancelled before the suspension started`，当轮继续响应用户 |
| superseded | 同轮第二次 `sleep_until` | `Superseded by a later sleep_until call` |

### 各入口与挂起的交互

| 入口 | 行为 |
|---|---|
| 到点 tick | `resume_from_sleep(准点/迟到)`，唤醒 run 无 user 消息——交付的工具结果即新输入 |
| 用户发消息（挂起中，idle） | `start()` run 序言交付延迟结果（标注被打断，位于用户消息之前），取消挂起，模型当轮响应；是否再睡由模型自己决定（重新调用 `sleep_until` 即重新挂起） |
| 用户 steer（挂起轮批尾执行中） | run 循环顶部交付延迟结果（标注批内取消）并继续当轮 |
| 停止按钮（挂起中） | `/interrupt` → `TimerService.abort`：交付延迟结果（标注已中断），**不启动唤醒 run**——与普通工具调用被停止同构，会话回到空闲等待输入 |
| 停止按钮（运行中） | 正常中断；Cancelled 处理器同时丢弃已武装的挂起（未应答调用补 `[interrupted]`，timer 清除），不会留下孤儿唤醒 |
| `POST /wake` | 手动立即唤醒；未挂起 409，agent 运行中 409（唤醒已挂起、run 结束后自动触发，发消息可立即 steer） |
| `/compact` | 挂起中发送即用户消息路径（打断挂起）；合并后的 turn 若是压缩轮，压缩在交付结果后照常进行 |
| `/revert` | 回滚历史同时清内存标志与挂起（挂起轮是最后一轮，任何回滚点都将其整体截除） |
| 删除 session | `TimerService.remove_session`（丢弃 pending），目录随级联删除 |
| 模式切换（Tab / `/mode`） | 允许；`session.json` 读-改-写保留 `sleep_until` 字段，唤醒时按新模式运行（`<modeswitch>` 通知随唤醒 run 注入） |
| 服务重启 | `load_all` 重载 pending（含过期目标）；停机期间到点的目标在启动后 1 秒内补触发（迟到唤醒）。重启后用户在补触发前发消息：run 序言经 `is_sleeping` 检查取消挂起，定时唤醒不再触发 |

### 重启恢复

延迟结果不持久化：恢复实例上定位未应答的 `sleep_until` 调用靠**扫描历史**（最后一条带未应答 sleep 调用的 `ModelMessage`），不依赖任何内存状态。交付时把结果插入既有 `ToolResults` 条目——若该轮已被回滚截除，扫描自然落空，静默返回。防重复交付：只有未应答的调用会补结果；硬取消路径补写的 `[interrupted]` 使唤醒扫描落空。

### CLI 不支持

CLI 的 IDLE 循环阻塞在同步 `input()`（事件循环停摆），tick 无法及时触发。CLI 模式创建的 Agent 不注入 TimerService，`sleep_until` 调用返回错误文本。

## 四、蓝点与前端

- **数据源**：`GET /api/sessions`（3s 轮询）对每个主 session 附加 `has_sleep` 与 `sleep_until`（epoch 秒，tooltip 显示"sleeping until <时间>"）。
- **状态点优先级**（`frontend/src/utils/sessionStatus.ts`）：运行（绿点闪烁）> 挂起（蓝点 `pending`）> 空闲（灰点）；project 行状态点由成员聚合。
- **排序**：运行中 > 挂起 > 最近活动 > 名字（rank 逻辑在 `SessionSidebar.vue` 的 `sortTop`，以 `has_sleep` 计 rank）。
- **SSE 事件**（进本 session 的 broker，不落历史）：
  - `sleep_start {session, until}`：挂起生效，前端即时置 `sleep_until`（蓝点不等 3s 轮询）。
  - `sleep_end {session}`：挂起结束，前端即时清除。
- **工具条目**：`sleep_until` 的调用在挂起期间是 pending 态，条目显示 `sleeping...`；唤醒时 `tool_result` 事件补全结果（跨 run 到达，前端按 `tool_call_id` 回落匹配）。重建历史时结果按 `tool_call_id` 与调用配对（执行重排使结果位置与声明顺序不同），未应答调用标记为 pending。
- **停止按钮**：挂起中的会话停止按钮保持红色可按——按下即中断挂起（交付"已中断"结果，无唤醒 run），会话回到空闲等待输入，与普通工具调用被停止同构；仅空闲态置灰。
- **挂起横幅**：当前查看的会话处于挂起时，顶部显示 `⏳ 挂起至 <时间>` 横幅 + **立即唤醒**按钮（调 `/wake`）；输入框保持可用（发消息即打断挂起，语义一致）。

## 五、持久化

- `session.json` 字段 `sleep_until: <epoch 秒>`（挂起中存在，唤醒/取消时删除）。无独立数据文件。
- 延迟结果不持久化：唤醒时从历史扫描构造并插入 `ToolResults`，随常规路径落 `history.json`。
- 唤醒不重建 system prompt、不插入 CompactMarker：挂起只是"时间上的暂停"，上下文原样保留。

## 六、权限与安全

- **仅主 agent 可用**：`sleep_until` 由 `BuildModePermission` 与 `PlanModePermission` 共同暴露与放行（两模式暴露同一全量工具前缀，缓存稳定）；subagent 角色（explore/general）工具集不含它，`intercept` 兜底拦截——有界任务不允许睡眠（父 agent 会挂起等待）。
- **无资源消耗**：挂起仅是一个时间戳 + 每秒一次的字典扫描；不占用 LLM 连接、线程或进程。
- 上限：单次 ≤365 天、≥5 秒；每 session 至多一个挂起（后设覆盖）。

## 七、Non-goals

- ❌ 周期性调度（cron 语义）：需要重复行为时，模型醒来后再次调用 `sleep_until` 即可（每次睡眠都是一次显式决策，附上下文）。
- ❌ subagent / CLI 睡眠（见上文）。
- ❌ 唤醒结果通知其他 session：唤醒只发生在本 session（不存在 fire-and-forget 的跨 session 语义）。
- ❌ 修改挂起时间：要调整就打断挂起后重新睡（停止按钮 / `/wake` / 发消息）。

## 八、实现拆解

1. **`src/trilobite/timer.py`**：`TimerService`——pending 表 + tick 循环 + `load_all`/`register`/`cancel`/`remove_session`/`is_sleeping`/`sleep_until`/`wake`/`abort`/`_do_wake`；`parse_sleep_until` 时间解析；`sleep_result_text` 延迟结果文案（原因 × 时钟判定）；`session.json` 的 `sleep_until` 字段读-改-写。
2. **`src/trilobite/tool_call.py`**：`SLEEP_UNTIL_DEF`（虚拟工具定义，描述含执行顺序与打断语义）。
3. **`src/trilobite/permission.py`**：主模式（build/plan）暴露并放行 `sleep_until`。
4. **`src/trilobite/agent.py`**：`timer_service` 构造参数、`_sleeping_until` 状态、工具循环的批尾重排与 sleeping 分支（跳过结果落盘）、run 循环顶部的挂起 break 与批内取消、run 序言的用户打断路径、`_deliver_sleep_results`（历史扫描 + ToolResults 插入 + 事件）、`resume_from_sleep`（唤醒 run 入口）、硬取消路径的挂起清理。
5. **`src/trilobite/prompts.py`**：system prompt 的 timer 段（延迟结果语义、批尾执行、打断后自决再睡）。
6. **`src/trilobite/server.py`**：TimerService 装配（startup `load_all`+`start`）、`GET /api/sessions` 的 `has_sleep`/`sleep_until`、`POST /wake`、`/interrupt` 的挂起 abort 分支、`/revert` 取消挂起、删除 session 级联。
7. **`src/trilobite/cli.py`**：不注入 TimerService（`sleep_until` 报错路径）。
8. **前端**：`types.ts`（`sleep_start`/`sleep_end` 事件、Session 的 `has_sleep`/`sleep_until`）、`store.ts`（事件处理、重建历史按 id 配对工具结果、未应答调用标 pending、跨 run `tool_result` 回落匹配）、`sessionStatus.ts`（蓝点语义）、`SessionSidebar.vue`（排序字段）、`App.vue`（挂起横幅 + 立即唤醒）、`ChatInput.vue`（挂起中停止按钮保持可按）、`ToolEntry.vue`（`sleeping...` 条目）。
