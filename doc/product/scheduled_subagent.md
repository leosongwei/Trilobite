# 定时 Subagent（Scheduled Subagent / Cron）

> 状态：**设计稿，待审核**。审核通过后再实现。本文是定时 subagent 功能的产品规格，参考调研见第二节（opencode、kimi-code）。

## 一、概述

定时 subagent 让主 agent 能把一段 prompt 挂到 cron 时间表上，到点后系统自动派生一个**无人值守的定时 agent** 独立跑完这段任务。它和 `task` 工具派生的 subagent 的关键区别是：

- **不返回结果**：fire 是一次"发射后不管"（fire-and-forget）。定时 agent 的输出**永远不回填主 agent 的上下文**，主 agent 只通过 `cron_create`/`cron_list`/`cron_delete` 三个工具管理时间表本身（增、删、查，**不能改**）。主上下文零污染，主 agent 不必为空闲时段的定时任务预留 token。
- **周期复用**：同一个 schedule 每次 fire 复用同一个 session，历史跨多次运行累积，可长期回看。
- **跨重启存活**：schedule 持久化在磁盘，服务重启后调度器重载，继续按表触发。

典型用例：每天定时检查依赖更新并写入 CHANGELOG、每小时把某目录的 grep 结果归档、明天下午提醒用户做某件事（一次性）。

### v1 范围（本次实现）

- ✅ 三个工具 `cron_create` / `cron_list` / `cron_delete`（增删查，无修改语义）。
- ✅ 5 字段 cron 表达式（本地时区）+ prompt（≤ 8KB）+ `recurring` 标志（默认 true；`false` 为一次性，fire 后自动删除）。
- ✅ fire 时派生定时 agent：独立 Agent 实例、独立 session、`max_steps` 100 有界运行、结果不返回主 agent。
- ✅ 每次 fire 全新上下文（fire 边界复用 `CompactMarker` 机制裁剪），历史全量保留在磁盘供查看。
- ✅ 侧边栏树状展示：主 session 下挂定时 session 节点（时钟徽标），可切入查看、可中断正在运行的 fire。
- ✅ schedule 持久化到主 session 目录 `schedules.json`，服务重启重载，只计未来触发（停机期间错过的 fire 不补跑）。
- ✅ 无人值守权限：工作区（含 `additional_dirs`）内全权限，越界**自动拒绝**（不弹交互式审批，见第六节）。
- ✅ 派生角色按主 agent 模式固化：build → general，plan → explore（与 `task` 工具的派生权限规则一致）。

### v1 不做的事（non-goals）

- ❌ 修改既有 schedule（改 cron / 改 prompt / 开关）。要调整就 `cron_delete` + `cron_create`。
- ❌ 定时 agent 的 steering。fire 运行中只支持中断（■），不支持发消息引导；idle 期间 `/message` 一律拒绝。
- ❌ fire 结果回填主 agent（任何形式的注入都不做，包括 synthetic 消息通知）。
- ❌ 错过补偿：停机/繁忙期间错过的 fire 不补跑（不 coalesce）。
- ❌ 多级嵌套：定时 agent 看不到 `task` 工具，无法再派生。
- ❌ 全局并发上限、jitter 错峰（单用户本地应用无 herd 问题）。
- ❌ schedule 过期机制（kimi-code 的 7 天 stale 不采用，删除是显式操作，见第十一节）。

## 二、参考调研

**opencode**：无内置定时功能。仅有实验性 background subagent（`task` 工具 `background: true`，`OPENCODE_EXPERIMENTAL_BACKGROUND_SUBAGENTS=1` 开启）：子 agent 转后台后 `task` 工具立即返回，完成时自动发 `task.update` 事件通知，agent 可用 `task_id` 续接；纯内存注册表、无持久化，session 关闭时自动取消其后台任务。另有 GitHub Actions 集成支持外部 cron 触发仓库任务，但 opencode 自身不含定时器。

**kimi-code**：有 "Scheduled tasks"（cron 定时任务），是我们主要参考：

- 三个内置工具 `cron-create` / `cron-list` / `cron-delete`（增删查，无修改）。
- 参数：5 字段 cron 表达式（本地时区）+ prompt（≤ 8KB）+ `recurring`（默认 true，false 为一次性自删）；每 session 上限 50 个。
- 调度：`SessionCronService` 轮询 tick（1s），到点经 `IAgentPromptService.inject` 把 prompt 以 `<cron-fire>` user 消息**注入主 agent** 触发新一轮执行（这是与我们的关键差异：kimi 在**主 agent 上下文里**执行，结果进主 transcript；我们派生**独立 agent**，结果不进主上下文，对应"定时 agent 不返回结果"的需求）。
- 生命周期：recurring 7 天过期（stale 标记），一次性 fire 后自删；持久化到 `cron/<workspaceId>/<id>.json`，恢复同 session 时重载。
- 附带确定性 jitter、错过 fire coalesce 合并、agent 运行中 buffered。

我们的设计在"三个增删查工具 + cron 表达式 + prompt + recurring"上与 kimi-code 保持一致，在执行模型上改为独立定时 agent（更贴 Trilobite 的 subagent 架构与"不返回结果"需求），并在持久化位置（schedule 随主 session）、生命周期（无过期、无 coalesce）上做了简化决策，见第十一节。

## 三、概念定位

定时 subagent 与 subagent 一样是**角色**而非**模式**：role（explore/general）在创建 schedule 时按主 agent 当前模式固化，之后不切换。它与 `task` subagent 的差异：

| 维度 | `task` subagent | 定时 subagent |
|---|---|---|
| 派生者 | 主 agent 的 `task` 工具调用 | 调度器按 cron 到点自动触发 |
| 结果 | `<task_result>` 回填主 agent | **不返回**，主 agent 无感知（除侧边栏） |
| 生命周期 | 有界，结束即 sealed，不可复用 | 每次 fire 复用同一 session，fire 间 idle |
| 上下文 | 零起点，单次运行 | 每次 fire 全新上下文（marker 裁剪），历史跨 fire 累积展示 |
| 权限请求 | 交互式（全局横幅审批） | 无人值守，越界自动拒绝 |
| steering | 运行中可 steer | 不支持（只可中断） |
| 持久化 | 无（进程内，重启即失） | schedule 落盘，重启重载续跑 |
| 工具集 | 无 task / todo / exit_plan_mode | 同左 |

共同点：均为一层嵌套（无 `task` 工具）、`max_steps` 100 有界运行、侧边栏树状挂载于主 session 之下、可中断。

## 四、三个 cron 工具

三个**虚拟工具**（仅 LLM 可见定义，执行逻辑在 `Agent` 内，与 `exit_plan_mode`/`task` 同路径），仅暴露给主 agent（build/plan 模式），**不暴露给子 agent 与定时 agent**。增删查三态由工具集本身表达，"不能改"没有对应的修改工具，天然不可达。

### `cron_create`

```jsonc
{
  "cron": "30 9 * * *",      // 5 字段 cron 表达式，本地时区（分 时 日 月 周）
  "prompt": "…",             // 1..8192 字节，fire 时作为任务说明注入定时 agent
  "recurring": true          // 默认 true；false = 下次匹配时 fire 一次后自动删除
}
```

返回：`{id, cron, recurring, next_fire_at, description}`。`description` 为 prompt 前 40 字符（空白折叠），用作侧边栏展示名。

校验：cron 表达式可解析（用 `croniter`）；prompt 非空且 ≤ 8KB；每 session 上限 20 个（超出返回错误）；`recurring=false` 时 cron 必须能在未来匹配到（超出 5 年窗口视为不可达，报错）。

### `cron_list`

无参数。返回该 session 的全部 schedule：`{id, cron, recurring, description, next_fire_at, run_count, last_state, last_fire_at, prompt 预览(≤200 字符)}`。`next_fire_at` 为 null 表示 cron 在 5 年窗口内不再匹配（recurring=false 且已 fire 的 schedule 已被自删，不会出现）。

### `cron_delete`

参数：`{id}`。删除 schedule 并停止未来触发。**历史 session 保留**（可继续查看过去的运行记录，节点标记为已删除不再触发），用户可另行删除 session。删除不存在的 id 返回错误。

### 派生角色规则

`cron_create` 执行时按主 agent 当前 permission 固化定时 agent 的角色（写入 schedule）：

| 主 agent 模式 | 定时 agent 角色 |
|---|---|
| Build | `general`（工作区内全权限） |
| Plan | `explore`（只读） |

与 `task` 工具的派生权限规则一致，保住 plan 模式只读语义不被"借壳"破坏。角色在创建时固化，之后主 agent 切模式不影响已创建 schedule 的 fire。

## 五、调度与生命周期

### CronService（新模块 `scheduler.py`）

全局单例，server 持有：

- **加载**：启动时扫描 `sessions/*/schedules.json` 载入全部 schedule（按 session 聚合）。
- **tick**：asyncio 循环每秒一次（可配置），对每个 schedule 计算 `croniter` 下次触发时间，到点触发 fire。
- **增删**：`cron_create`/`cron_delete` 工具经 Agent 调用 service 的 `create`/`delete`，同步落盘 `schedules.json` 并更新内存表。
- **session 删除联动**：主 session 删除时调 `remove_session(session_name)`，其 schedule 一并清除。

### fire 流程

1. tick 命中且该 schedule 无运行中的 fire（有则跳过，发 `cron_missed` 事件，见第七节）。
2. 取/建定时 Agent 实例（名字 = schedule 对应 session 的 id）：
   - `agents` 字典中已有实例（上次 fire 结束后的 idle 实例）→ 直接复用；
   - 没有（重启后或首次 fire）→ 按 `session.json`（working_dir、role、parent_session）从磁盘重建并注册进 `agents` 字典。
3. 在定时 session 的 history 末尾追加一个 `CompactMarker` + 一条合成 user 消息：`⏰ 定时触发（<本地时间>）\n<prompt>`。
4. 以独立 asyncio task 跑 `run()`（与主 agent 运行互不阻塞）。
5. fire 结束（完成/中断/出错/超步数）→ 更新 `schedules.json` 的 `run_count`/`last_state`/`last_fire_at`，发 `cron_fire_end` 事件，实例留在 `agents` 字典供查看。

### 每次 fire 全新上下文

`get_api_messages` 从最后一个 `CompactMarker` 之后取消息，fire 边界插入的 marker 天然把 API 上下文裁剪为**本次 fire** 的消息（fresh context，无跨 run 污染、无累积 token 膨胀）。marker 之前的历史仍完整保存在 `history.json` 供前端展示；前端在定时 session 视图中把 marker 渲染为"定时运行"分隔线（区别于主 session 的压缩分隔线，见第七节）。

### 中断与取消

- **用户中断**：定时 session 视图的 ■ 走 `POST /api/sessions/{id}/interrupt`，复用 `agent.interrupt()`：kill 在飞 bash 进程组、cancel run task、产出中断总结、以 `interrupted` 终态结束本次 fire。定时 agent **不 sealed**，下次 fire 正常复用。
- **主 agent 停止**：定时 fire 是独立 asyncio task，不在主 run 的 `gather` 之内，主 agent 的 cancel **不传播**给它——定时任务继续跑完（"发射后不管"语义）。
- **服务重启**：运行中的 fire 丢失（与 subagent 同，进程内态），history 已落盘可回看；schedule 重载后只计未来触发。

## 六、无人值守权限

定时 agent 在无人值守环境运行，**不弹交互式权限请求**（没人审批会挂死）：

- 工作区 + 继承主 session 的 `additional_dirs` 范围内：正常执行（general 角色全工具，explore 角色只读）。
- 越界访问：工具直接返回错误（与 subagent 的交互式审批不同，定时 agent 的 `intercept` 对越界路径直接拒绝），错误信息会进入该次 fire 的历史，用户回看可知。
- 敏感文件过滤沿用 `file_access.py`。

## 七、事件与前端

### 事件（主 session broker）

| 事件 | 时机 | 载荷 |
|---|---|---|
| `cron_fire` | fire 开始（spawn 后即发） | `{schedule_id, session, description, state:"running"}` |
| `cron_fire_end` | fire 结束（逐个增量） | `{session, state}`（completed / interrupted / error） |
| `cron_missed` | 上一 fire 未结束，本次跳过 | `{schedule_id, session}` |

fire 事件**只进主 session 的 broker**（主时间线不注入任何聊天内容，不落历史）；定时 agent 自身的输出只进自己 session 的 stream。`cron_create`/`cron_list`/`cron_delete` 是普通工具调用，走既有的 `tool_start`/`tool_result` 渲染。

### 侧边栏树

- 定时 session 节点挂载于主 session 之下（`parent_session` 字段，与 subagent 同），带**时钟徽标**（与 EX/GE 角色徽标区分），描述显示 prompt 预览；节点信息展示 cron 表达式、`next_fire_at`、`run_count`、最近一次 `last_state`。
- fire 运行中：running 徽标；完成后回到 idle（不显示 sealed，区别于 subagent）。
- schedule 已删除的 session：标记"已停止"（不再有 fire），历史仍可查看。
- 定时 session 不参与主 session 的自动命名；排序同 subagent（`created_at` 降序）。

### 定时 session 视图（复用 ChatView）

- 顶部 bar：时钟徽标 + schedule 描述 + 返回父会话导航；运行中显示 ■ 停止按钮（走 interrupt）。
- **无输入框**（不支持 steering）：idle 与运行中都不显示；`POST /message` 后端拒绝（"定时 agent 不接受输入"）。
- fire 之间的历史以"定时运行"分隔线分段展示；marker 之前的旧运行可向上翻看。
- `store.ts` 处理 `cron_fire`/`cron_fire_end`/`cron_missed` 事件，维护定时节点的 `isRunning`/`run_count`/`last_state`。

## 八、持久化

### `schedules.json`（主 session 目录，与 `todos.json` 同级）

```jsonc
{
  "version": 1,
  "schedules": [
    {
      "id": "…",              // uuid4().hex
      "cron": "30 9 * * *",
      "prompt": "…",
      "recurring": true,
      "role": "general",       // 创建时按主 agent 模式固化
      "description": "…",      // prompt 前 40 字符
      "created_at": "…",
      "run_count": 12,
      "last_state": "completed",   // 最近一次 fire 终态；从未 fire 为 null
      "last_fire_at": "…"
    }
  ]
}
```

### 定时 session（`sessions/<uuid>/`）

与 subagent 同构，`session.json` 增加：`kind: "scheduled"`、`schedule_id`、`role`；沿用 `parent_session`（主 session id）、`created_at`。一个 schedule 对应**唯一** session，每次 fire 复用，history 跨 fire 累积（`CompactMarker` 分段）。

### 重启恢复

启动时 `load_all()` 从磁盘重载 schedule 并重新注册 tick。错过的触发点不补跑（服务恢复后从下一个未来匹配点继续）。

## 九、限制与安全

- 每 session 上限 **20** 个 schedule（防止 LLM 失控创建；超出 `cron_create` 返回错误）。
- prompt ≤ 8KB；cron 必须可解析且（一次性）在 5 年窗口内有匹配。
- 无人值守自动拒绝越界（见第六节）；定时 agent 无 `task`/`todo`/`exit_plan_mode`（一层限制）。
- `max_steps` 100 兜底防失控。
- schedule 随主 session 删除而删除；删除 schedule 保留历史 session（用户可再删 session）。
- 定时 fire 消耗的 token 与主 agent 无关，全部计入定时 session（用户可在会话信息面板看到各 session 用量）。

## 十、实现拆解（审核通过后执行）

1. **`pyproject.toml`**：新增依赖 `croniter`（cron 解析与下次触发计算）。
2. **`scheduler.py`**（新模块）：`CronService`——`load_all` / `create` / `delete` / `list` / `remove_session` / `tick` 循环 / `_fire`（重建或复用定时 Agent 实例、发事件、更新统计）。构造时注入 session 根目录、config、`agents` 注册表、Agent 工厂（或 server 提供回调）。
3. **`tool_call.py`**：新增 `CRON_CREATE_DEF` / `CRON_LIST_DEF` / `CRON_DELETE_DEF` 三个虚拟工具定义（描述采用正向框架，见 prompts 一条）。
4. **`permission.py`**：`BuildModePermission` / `PlanModePermission` 的 `tool_names` 增加三个 cron 工具；Explore/General 不含。
5. **`agent.py`**：工具派发处加三个分支（`cron_create`/`cron_list`/`cron_delete`），执行逻辑调 `self._cron_service`（server 构造主 agent 时注入）；`cron_create` 按当前 permission 固化 role。
6. **`server.py`**：创建 `CronService`（启动时 `load_all`，生命周期内跑 tick task）；删除 session 时联动 `remove_session`；`/message` 对定时 session 拒绝（idle 与 running 均拒，`kind == "scheduled"` 即拒）。
7. **`prompts.py`**：`SYSTEM_PROMPT` 增加 cron 工具指引（正向框架：周期性/无人值守/提醒类任务用 cron；需要结果回填的探索用 `task`；一次性临时任务直接对话；要调整先 delete 再 create）。三个工具定义描述与之一致。
8. **前端**：`store.ts` 处理 `cron_fire`/`cron_fire_end`/`cron_missed` 事件；侧边栏树渲染定时节点（时钟徽标、cron、运行状态、run 统计）；定时 session 视图（复用 ChatView，无输入框，运行中 ■ 停止）；`ChatView` 对定时 session 把 `compact` 分隔线渲染为"定时运行"分隔线。

## 十一、风险与决策记录

- **独立 agent vs 注入主 agent**：kimi-code 把 `<cron-fire>` 注入主 agent 上下文执行（结果进主 transcript）。我们选独立定时 agent：符合"不返回结果"需求、主上下文零污染、主 agent 无需为空闲时段任务预留 token；代价是每次 fire 是完整独立 run（冷启动、无主上下文继承），且无人值守场景需自动拒绝越界。定时任务本就应自包含（prompt 携带全部所需信息），与 subagent 的上下文隔离哲学一致。
- **fire 边界复用 CompactMarker**：`get_api_messages` 天然从最后一个 marker 裁剪，零新机制实现"每次 fire 全新上下文"；marker 之前的历史留在磁盘供前端分段展示。副作用是定时 session 若单次 fire 内触发 compaction 也正常（多插一个 marker 而已）。
- **一次 fire 一个 session 累积 vs 每次新建 session**：选累积——树节点稳定、历史可纵向对比（"这周每天都干了什么"），且避免侧边栏被 fire 刷屏。
- **无过期机制**：kimi 的 7 天 stale 服务于其注入式模型（需要刷新机制防累积）；我们每次 fire 独立上下文无累积问题，删除是显式操作，语义更干净。
- **无错过补偿**：kimi 会 coalesce 停机期间错过的 fire；我们服务重启后只计未来触发。本地单用户应用错过即错过（重启恢复时立即补一次反而突兀）。
- **无 jitter / 全局并发上限**：单用户本地应用无 herd 问题；多 schedule 并发 fire 是独立 asyncio task，资源由用户自担。同一 schedule 不重叠（重叠跳过并发 `cron_missed` 事件）防止 self-叠加。
- **增删查三工具而非单一 cron 工具 + action 枚举**：与 kimi-code 一致；每个工具 schema 最小、LLM 一次调用即达，"不能改"由工具集缺失表达。
- **每 session 20 个上限**：比 kimi 的 50 保守——每个 schedule 对应一个持久 session 与周期运行，资源占用比 kimi 的注入式重；上限可调。
- **无人值守自动拒绝越界**：定时 agent 不弹交互式审批（无人审批会挂死），越界直接返回错误进 fire 历史。用户可预先把目标目录加进主 session 的 `additional_dirs`（定时 agent 继承）。
- **主 agent 停止不传播给定时 fire**：定时 fire 不在主 run 的 `gather` 内，独立生命周期；"发射后不管"语义明确。
