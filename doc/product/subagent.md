# Subagent（子代理）

> 状态：**设计稿，待审核**。审核通过后再实现。本文是 subagent 功能的产品规格，研究背景见 `doc/subagents/` 下两篇讨论稿。

## 一、概述

Subagent 是"主 agent 通过一次 `task` 工具调用派生出来的子 agent"。主 agent 在对话中调用 `task` 工具，传入**一组**子任务，系统为每个子任务新建一个独立的子 Agent 实例（独立上下文、独立 session），**并行**跑完，`gather` 所有子 agent 的结果后，作为这一次工具调用的结果回填给主 agent。对主 agent 来说，它只拿到一段汇总文本；对用户来说，每个子 agent 是一条可以点进去看的独立会话。

核心价值：**隔离上下文、省主 agent token、并行**。把"探索/搜索"这类吃上下文的活外包给子 agent，主 agent 只回收结论。

### v1 范围（本次实现）

- ✅ `task` 工具，一次调用可派生**多个**子 agent 并行运行，`gather` 全部结束后返回（前台同步模式）。
- ✅ 前端树状展示：主会话里 `task` 节点列出各子 agent，可点开进入子 session 视图。
- ✅ 仅允许**一层**：子 agent 看不到 `task` 工具，无法再派生。
- ✅ 用户可**中断**某个子 agent，中断后它立刻产出总结再退出。
- ✅ 子 agent 上下文隔离：只拿到主 agent 给的 prompt，不继承主 agent 历史。

### v1 不做的事（non-goals）

- ❌ 后台模式（父 agent 不阻塞、子 agent 完成后注入 synthetic 消息通知）。
- ❌ 续跑（复用已有子 session 继续）。
- ❌ `@mention` 入口（用户直接把消息路由给子 agent）。
- ❌ 多层嵌套（子 agent 派生子 agent）。
- ❌ 主 agent 与子 agent 之间的中途通信 / steering（子 agent 跑起来后，主 agent 不再向它发消息）。
- ❌ 细粒度 deny/ask/allow 权限 ruleset（沿用 `permission.py` 的工具白名单即可）。

## 二、概念定位：角色，不是模式

Subagent 是一个**角色**（role），不是**模式**（mode）--这是 `permission.py` 已确立的分辨：

- **模式**（plan/build）：主 agent 的运行时状态，会话中途热切换。
- **角色**（explore/general）：subagent 的声明式 profile，**派生时固化、自始至终不切换**。

子 agent 用 `ExploreSubagentPermission` / `GeneralSubagentPermission`（已定义），不涉及 plan/build 模式。主 agent（`BuildModePermission` / `PlanModePermission`）持有 `task` 工具；子 agent 的权限白名单**不含 `task`**，从而硬性限制一层。

## 三、`task` 工具

### 工具定义

`task` 暴露给主 agent（build/plan 模式），**不暴露给子 agent**。它的执行是异步的（需 `await` 多个子 agent 运行），且需要主 agent 上下文，因此**不走**通用的 `tool_call.execute_tool` 同步路径，而是在 `Agent` 的工具派发处特判处理（与 `exit_plan_mode` 同样的处理方式）。

### 参数

```jsonc
{
  "tasks": [
    {
      "description": "3-5 词标签，用作树节点和子 session 标题",
      "subagent_type": "explore",   // "explore" | "general"
      "prompt": "高度自包含的任务说明：目标、涉及的文件/路径、要求返回什么"
    }
    // ...1 个或多个，全部并行
  ]
}
```

- `tasks` 是数组，**单次调用即可 fan-out 多个子 agent**。只派生一个时传长度为 1 的数组。
- `prompt` 必须自包含（路径、目标、返回格式都写清），因为子 agent 拿不到主 agent 的历史。
- `subagent_type` 决定子 agent 的 permission：`explore`（只读 read/bash）、`general`（可写 read/write/bash）。

### 执行流程

1. 校验每个 `subagent_type` ∈ {explore, general}；非法者直接返回错误项，不阻塞其它。
2. 对每个子任务，新建子 Agent（见第四节），把 `prompt` 作为子 agent 的第一条 user 消息。
3. `asyncio.gather` 并发跑所有子 agent 的 `run()`，**全部结束后**统一回收。
4. 取每个子 agent history 最后一条 assistant 文本作为该子 agent 的结果。
5. 把所有结果组装成 `<task_result>`（见第七节）作为本次工具调用的 result 回填主 agent。

主 agent 在 `task` 工具执行期间**阻塞**（前台模式）。子 agent 的中间过程不进主 agent 的时间线，只进各自 session 的 stream。

## 四、子 Agent 生命周期

### 创建

复用 `Agent.__init__`，传入：

| 字段 | 取值 |
|---|---|
| `name` | `f"{parent_name}__{shortid}"`（shortid = uuid4 hex 前 8 位，保证唯一） |
| `working_dir` | 与父 agent 相同（同一工作区） |
| `session_dir` | `sessions/<child_name>/`（扁平结构，与父平级） |
| `config` | 继承父 agent 的 config（同一模型/API） |
| permission | `ExploreSubagentPermission()` 或 `GeneralSubagentPermission()`，按 `subagent_type` |
| `depth` | `parent.depth + 1`（父为 0，子为 1） |

子 agent 的 `system_prompt` = 基础 system prompt + **TASK_AGENT_ROLE_PREFIX**（公共前缀，见下）+ 角色提示。

### TASK_AGENT_ROLE_PREFIX（公共前缀）

所有子 agent 的 system prompt 拼一段固定前缀（抄 kimi-code，统一不漏）：

> 你正在作为 subagent 运行。所有 `user` 消息来自主 agent。主 agent 看不到你的上下文，只能看到你完成任务后的最后一条消息。把主 agent 当作你的调用方。不要直接向终端用户提问；不清楚的在总结里说明。

### 上下文隔离

子 agent history 从零开始：system 消息 + prompt（第一条 user）。**不继承**父 agent 的任何历史。这是省 token 的关键。

### 权限派生与文件访问

- 子 agent 的工具集由其 permission 白名单决定（explore: read/bash；general: read/write/bash）。**不含 `task`**（单层限制）、**不含 `TodoList`**（子 agent 不维护用户 todo）、**不含 `exit_plan_mode`**（子 agent 无 plan/build 模式）。
- 父 agent 的 `additional_dirs`（已批准的外部目录）**透传**给子 agent。
- 子 agent **不发起交互式权限请求**：若 read/write/bash 企图访问工作区 + 继承的 additional_dirs 之外的路径，直接返回错误（"subagent 无权访问该路径"），而不是弹审批横幅阻塞（子 agent 没有可审批的 UI 通道）。即子 agent 的 `permission_request` 流程被关闭，越界即拒。

### plan 模式下的限制

主 agent 在 plan 模式也能调用 `task`（并行探索对规划有用），但**只能派生 `explore`（只读）子 agent**。`task` 工具执行时检查父 agent 当前 permission：若为 `PlanModePermission`，`subagent_type=general` 直接被拒（返回错误项）。这保证 plan 模式的只读语义不被"借壳"破坏。

## 五、并行与 gather

- 同一次 `task` 调用的多个子 agent 用 `asyncio.gather` 并发运行。
- `gather` 等待**全部**子 agent 结束（正常完成 / 被中断 / 出错）后才返回。任意一个子 agent 的失败或中断**不影响**其它子 agent 继续跑。
- `task` 是系统里第一个异步工具。`Agent` 工具派发处对 `task` 走 `await self._run_subagents(args)`；其余工具仍走同步 `execute_tool`。不在 v1 把 `execute_tool` 整体异步化。

### 中断与取消的传播

- **子 agent 中断**（用户在子 session 视图点 Interrupt）：只影响该子 agent，它产出总结后退出（见第六节）；其它子 agent 不受影响，`gather` 照常等它们完成。
- **主 agent 取消**（用户停掉主会话）：取消 `task` 工具的 `await`，并向所有运行中的子 agent 发取消信号，子 agent **硬停**（不产出总结），本次 `task` 调用整体作废、不回填结果。

## 六、中断 -> 总结 -> 退出

用户可在子 session 视图点 **Interrupt**，触发 `POST /api/sessions/{child}/interrupt`。行为：

1. 后端调用 `child_agent.interrupt()`，置中断标志并取消子 agent 当前在途的工具调用。
2. 子 agent 的 run 循环在**下一个安全点**（当前 LLM 流结束 / 当前工具调用被取消后）检测到中断标志，**跳出工具循环，不再发起新的工具调用**。
3. 执行**一个总结 turn**：以 user 消息注入"你被中断了。请简明总结你目前的发现/进展，然后停止。"，做一次无工具的 LLM 调用，把输出作为子 agent 的最终 assistant 消息。
4. 子 agent 发出 `interrupted` 事件并退出 run。
5. 主 agent 的 `task` 工具取这条总结作为该子 agent 的结果（`state="interrupted"`）。

"立刻产生总结"指：不再做新的工具活，尽快进入总结 turn。已开始在途的 LLM 流允许其流完（或在下个 token 边界截断），不强行半句话截断。

## 七、结果格式

`task` 工具的 result（回填主 agent 的 tool message）：

```xml
<task_result>
<subagent session="parent__a1b2" type="explore" description="查找 API 入口" state="completed">
<result>...子 agent 最后一条 assistant 文本...</result>
</subagent>
<subagent session="parent__c3d4" type="explore" description="梳理 DB schema" state="interrupted">
<result>...中断总结...</result>
</subagent>
<subagent session="parent__e5f6" type="general" description="重构 parser" state="error">
<result>...错误信息...</result>
</subagent>
</task_result>
```

`state` ∈ {`completed`, `interrupted`, `error`}。`session` 是子 session 名，前端据此跳转。

## 八、Session 结构与注册

### 目录结构（扁平）

```
sessions/
  <parent_name>/          # 主 session（现有）
    session.json          # 现有字段 + 新增 next_child_seq（可选）
    history.json
  <parent_name>__<shortid>/   # 子 session（与父平级）
    session.json          # parent_session, subagent_type, depth, description
    history.json
    agent.log
```

子 session 的 `session.json` 记录 `parent_session`（父名）、`subagent_type`、`depth`、`description`，用于前端标题和树归属。扁平结构（而非嵌套目录）与现有 session 模型兼容，`_get_or_create_agent` 等逻辑改动最小。

### 运行期注册

子 Agent 必须能被前端通过 `/stream`、`/history`、`/interrupt` 等 endpoint 访问。这些 endpoint 都走 server 的 `agents: dict[str, Agent]`。为避免 `agent.py` 反向依赖 `server.py`：

- `Agent.__init__` 增加可选参数 `registry: dict[str, Agent] | None`。server 创建主 agent 时把自己的 `agents` 字典传入；主 agent 创建子 agent 时把同一个 `registry` 传入，并在创建后 `registry[child_name] = child`。
- 这样子 agent 自动进入 server 的 `agents` 字典，现有 endpoint（按 name 查 `agents`）对子 session 直接可用。
- v1 为前台内存态：服务重启则运行中的子 agent 实例丢失（主 agent 同理），可接受。子 session 的 history 已落盘，重启后可只读回看。

## 九、Streaming 与前端

### 事件

主 agent 的 broker（父时间线）在 `task` 工具期间发：

| 事件 | 时机 | 载荷 |
|---|---|---|
| `tool_start` | `task` 开始 | `{tool:"task", args:{tasks:[...]}}` |
| `subagents` | 子 agent 全部 spawn 后 | `{parent, children:[{session, type, description, state:"running"}]}` |
| `subagent_state` | 某子 agent 结束时（可选，增量更新树状态） | `{session, state}` |
| `tool_result` | `gather` 完成 | `{tool:"task", result:"<task_result>...</task_result>"}` |

子 agent 的 broker 只发到自己 session 的 stream（`turn`/`thinking`/`text`/`tool_start`/`tool_result`/`done`/`interrupted`），**不**灌进父时间线。前端点开子 session 时订阅 `/api/sessions/{child}/stream`。

### 前端树状展示

- **主会话时间线**：`task` 工具渲染成一个节点，列出本次派生的所有子 agent，每项显示 `description` + `type` + 状态徽标（running/completed/interrupted/error），可点击。
- **子 session 视图**：复用 `ChatView`，但**隐藏输入框**（子 agent 不接受自由输入），顶部加"返回父会话"导航 + **Interrupt 按钮**。订阅子 session 的 SSE。
- **侧边栏**：子 session **不**出现在左侧 session 列表，只能从父会话的树节点点入。`store.ts` 维护 parent→children 映射。
- `store.ts`：处理 `subagents` / `subagent_state` 事件，维护会话树与子 session 的 `isStreaming` 状态。

## 十、可选增强（待你拍板是否纳入 v1）

以下来自讨论稿，**默认不纳入**，标注出来供审核时决定：

1. **summaryPolicy（结果蒸馏兜底）**：子 agent 跑完取最后一条文本后，若过短（如 < 150 字符），追加一条 user 消息让其扩写，最多重试 1 次。低成本高收益，但会增加子 agent 的 turn/token。这是系统内部的质量步骤，不属于"主 agent 向子 agent 通信"。
2. **max_steps**：给子 agent 设步数上限（如 explore 20、general 40），防死循环。超限返回 `<task_error>`。
3. **wall-clock 超时**：子 agent run 加超时（如 10 分钟），超时返回错误并附 resume 提示。
4. **并行上限**：`gather` 加 `Semaphore` 限制同时运行的子 agent 数（如 ≤ 4），超了排队。

我的建议：v1 至少纳入 **2（max_steps）** 作为防失控兜底；1/3/4 可后置。

## 十一、实现拆解（审核通过后执行）

1. **`permission.py`**：`BuildModePermission` / `PlanModePermission` 的 `tool_names` 增加 `task`；新增 `TASK_TOOL_DEF` 工具定义常量（或 `TaskTool` 子类），由这两个 permission 在 `filter_definitions` 里拼上。Explore/General 不变（天然不含 task）。
2. **`agent.py`**：
   - `__init__` 加 `registry` 与 `depth` 参数。
   - 工具派发处加 `elif tool_name == "task": tool_result = await self._run_subagents(args)`，作为 `intercept` 之后、`exit_plan_mode` 之后的分支。
   - 新增 `_run_subagents(args)`：校验、创建子 Agent（注入 prompt、permission、depth、registry）、`gather`、组装 `<task_result>`。plan 模式下拒绝 `general`。
   - 新增 `interrupt()`：置中断标志 + 取消在途工具调用，run 循环检测后做总结 turn。
   - 子 agent 的 `permission_request` 流程关闭（越界即拒）。
   - 主 agent 取消时传播取消给运行中的子 agent。
3. **`server.py`**：创建主 agent 时传 `registry=agents`；新增 `POST /api/sessions/{name}/interrupt`（调 `agent.interrupt()`）；`/stream`、`/history` 对子 session 复用现有逻辑。
4. **前端**：`store.ts` 会话树 + `subagents`/`subagent_state` 事件处理；`ToolEntry`（或新 `SubagentTree` 组件）渲染树节点；子 session 视图（复用 `ChatView`、隐藏输入、加 Interrupt + 返回导航）；侧边栏排除子 session。
5. **提示词**：`config_example/` 增加 `subagent_role_prefix.txt`（TASK_AGENT_ROLE_PREFIX）及 explore/general 角色提示；`system_prompt.txt` 增补 `task` 工具使用指引（强调：能直接 read/bash 搞定的别开 subagent；开 subagent 要给自包含 prompt；子 agent 输出对用户不可见，主 agent 需转述）。

## 十二、风险与决策记录

- **一次调用 fan-out vs 多次调用 gather**：选"一次 `task` 调用、`tasks` 数组 fan-out"（单次调用产出多个并行子 agent）。备选是"主 agent 一轮里发多个单子 agent 的 `task` 调用、由 run 循环 gather"（opencode 风格）。选前者是因为：单一 gather 点、单一中断句柄、结果按批结构化返回，且不必改造 run 循环去特判"同轮多 task 调用"。
- **task 工具异步化范围**：只让 `task` 异步，不把 `execute_tool` 整体异步化，控制改动面。
- **子 agent 权限请求**：关闭交互式审批（越界即拒），避免子 agent 阻塞在无人审批的权限请求上。
- **plan 模式派生**：允许 plan 派生 explore（只读），禁止派生 general，保住只读语义。
