# Subagent（子代理）

> 状态：**设计稿，待审核**。审核通过后再实现。本文是 subagent 功能的产品规格，研究背景见 `doc/subagents/` 下两篇讨论稿。

## 一、概述

Subagent 是"主 agent 通过一次 `task` 工具调用派生出来的子 agent"。主 agent 在对话中调用 `task` 工具，传入**一组**子任务，系统为每个子任务新建一个独立的子 Agent 实例（独立上下文、独立 session），**并行**跑完，`gather` 所有子 agent 的结果后，作为这一次工具调用的结果回填给主 agent。对主 agent 来说，它只拿到一段汇总文本；对用户来说，每个子 agent 是一条可以点进去看、可以中途引导的独立会话。

核心价值：**隔离上下文、省主 agent token、并行**。把"探索/搜索"这类吃上下文的活外包给子 agent，主 agent 只回收结论。

### v1 范围（本次实现）

- ✅ `task` 工具，一次调用可派生**多个**子 agent 并行运行，`gather` 全部结束后返回（前台同步模式）。
- ✅ 侧边栏**树状**展示：父 session 下挂子 session，可切入子 session 查看。
- ✅ 用户可对运行中的子 agent **steering**（中途发消息引导）；子 agent 是有界任务，**最终以总结结束，之后不再响应输入**（不可复用，但历史长期可查看）。
- ✅ 用户可**中断**子 agent，中断后立刻产出总结再退出。
- ✅ 仅允许**一层**：子 agent 看不到 `task` 工具，无法再派生。
- ✅ 子 agent 上下文隔离：只拿到主 agent 给的 prompt，不继承主 agent 历史。
- ✅ `max_steps` 默认 100，防子 agent 失控死循环。
- ✅ 子 agent 权限请求为**交互式**：弹提示、全局可见、写明是哪个子 agent 请求哪里的权限。

### v1 不做的事（non-goals）

- ❌ 后台模式（父 agent 不阻塞、子 agent 完成后注入 synthetic 消息通知）。
- ❌ 续跑 / 复用（复用已有子 session 继续）。子 agent 完成后只读可查，不再接受新输入。
- ❌ `@mention` 入口（用户直接把消息路由给子 agent）。
- ❌ 多层嵌套（子 agent 派生子 agent）。
- ❌ **主 agent** 向子 agent 中途通信（主 agent 跑起来后不主动给子 agent 发消息；但用户可直接 steer 子 agent）。
- ❌ 细粒度 deny/ask/allow 权限 ruleset（沿用 `permission.py` 的工具白名单即可）。

## 二、概念定位：角色，不是模式

Subagent 是一个**角色**（role），不是**模式**（mode）--这是 `permission.py` 已确立的分辨：

- **模式**（plan/build）：主 agent 的运行时状态，会话中途热切换。
- **角色**（explore/general）：subagent 的声明式 profile，**派生时固化、自始至终不切换**。

子 agent 用 `ExploreSubagentPermission` / `GeneralSubagentPermission`（已定义），不涉及 plan/build 模式，**没有 `exit_plan_mode`**（子 agent 就这两种角色，无模式可切）。主 agent（`BuildModePermission` / `PlanModePermission`）持有 `task` 工具；子 agent 的权限白名单**不含 `task`**，从而硬性限制一层。

## 三、`task` 工具

### 工具定义

`task` 暴露给主 agent（build/plan 模式），**不暴露给子 agent**。它的执行是异步的（需 `await` 多个子 agent 运行），且需要主 agent 上下文，因此**不走**通用的 `tool_call.execute_tool` 同步路径，而是在 `Agent` 的工具派发处特判处理（与 `exit_plan_mode` 同样的处理方式）。

### 参数

```jsonc
{
  "tasks": [
    {
      "description": "3-5 词标签，用作树节点和子 session 标题",
      "subagent_type": "explore",   // "explore" | "general"，仅这两种
      "prompt": "高度自包含的任务说明：目标、涉及的文件/路径、要求返回什么"
    }
    // ...1 个或多个，全部并行
  ]
}
```

- `tasks` 是数组，**单次调用即可 fan-out 多个子 agent**。只派生一个时传长度为 1 的数组。
- `prompt` 必须自包含（路径、目标、返回格式都写清），因为子 agent 拿不到主 agent 的历史。
- `subagent_type` 枚举恰好 {`explore`, `general`}，无其它（无 exit_plan_mode 概念）。

### 派生权限（按主 agent 模式）

| 主 agent 模式 | 允许的 `subagent_type` |
|---|---|
| Build | `explore`、`general` |
| Plan | 仅 `explore`（只读） |

`task` 工具执行时检查父 agent 当前 permission：若为 `PlanModePermission`，`subagent_type=general` 直接被拒（返回错误项），以保住 plan 模式的只读语义不被"借壳"破坏。

### 执行流程

1. 校验每个 `subagent_type` ∈ {explore, general} 且符合上表的派生权限；非法者直接返回错误项，不阻塞其它。
2. 对每个子任务，新建子 Agent（见第四节），把 `prompt` 作为子 agent 的第一条 user 消息。
3. `asyncio.gather` 并发跑所有子 agent 的 `run()`，**全部结束后**统一回收。
4. 取每个子 agent history 最后一条 assistant 文本作为该子 agent 的结果（中断者取其中断总结）。
5. 把所有结果组装成 `<task_result>`（见第七节）作为本次工具调用的 result 回填主 agent。

主 agent 在 `task` 工具执行期间**阻塞**（前台模式）。子 agent 的中间过程不进主 agent 的时间线，只进各自 session 的 stream。

## 四、子 Agent 生命周期

### 创建

复用 `Agent.__init__`，传入：

| 字段 | 取值 |
|---|---|
| `name` | `uuid4().hex`（纯 UUID，作为稳定标识兼目录名；子 session 没有人可读名字，前端用 `description` 展示） |
| `working_dir` | 与父 agent 相同（同一工作区） |
| `session_dir` | `sessions/<child_name>/`（扁平结构，与父平级） |
| `config` | 继承父 agent 的 config（同一模型/API） |
| permission | `ExploreSubagentPermission()` 或 `GeneralSubagentPermission()`，按 `subagent_type` |
| `depth` | `parent.depth + 1`（父为 0，子为 1） |
| `registry` | 父 agent 的 registry（同一个 `agents` 字典），见第八节 |
| `parent_broker` | 父 agent 的 broker 引用，用于权限请求全局广播（见第六节） |
| `max_steps` | 100（默认） |

子 agent 的 `system_prompt` = 基础 system prompt（`SYSTEM_PROMPT`）+ **`SUBAGENT_ROLE_PREFIX`**（公共前缀，见下）+ 角色提示，由 `prompts.subagent_system_prompt()` 拼接。三者均为 `src/trilobite/prompts.py` 中的代码常量。

### `SUBAGENT_ROLE_PREFIX`（公共前缀）

所有子 agent 的 system prompt 拼一段固定前缀（抄 kimi-code，统一不漏），即 `src/trilobite/prompts.py` 中的 `SUBAGENT_ROLE_PREFIX` 常量：

> 你正在作为 subagent 运行。所有 `user` 消息来自主 agent 或用户的 steering。主 agent 看不到你的上下文，只能看到你完成任务后的最后一条消息。把主 agent 当作你的调用方。不要直接向终端用户提问；不清楚的在总结里说明。你是一个有界任务，完成后以总结收尾。

### 上下文隔离

子 agent history 从零开始：system 消息 + prompt（第一条 user）。**不继承**父 agent 的任何历史。这是省 token 的关键。用户的 steering 消息会进入子 agent 自己的 history（属于子 session）。

### 工具与单层限制

子 agent 的工具集由其 permission 白名单决定（explore: read/bash；general: read/edit/write/bash）。**不含 `task`**（单层限制）、**不含 `TodoList`**（子 agent 不维护用户 todo）、**不含 `exit_plan_mode`**（子 agent 无模式）。因白名单不含 `task`，子 agent 结构上看不到派生工具 -> 单层硬限制。

## 五、并行、steering 与中断

### 并行与 gather

- 同一次 `task` 调用的多个子 agent 用 `asyncio.gather` 并发运行。
- `gather` 等待**全部**子 agent 结束（正常完成 / 被中断 / 出错 / 触发 max_steps）后才返回。任意一个的失败或中断**不影响**其它继续跑。
- `task` 是系统里第一个异步工具。`Agent` 工具派发处对 `task` 走 `await self._run_subagents(args)`；其余工具仍走同步 `execute_tool`（但在调用处用 `asyncio.to_thread` 丢到工作线程执行，避免 bash 的 `subprocess.run` 阻塞共享事件循环，详见 streaming.md）。不在 v1 把 `execute_tool` 整体异步化。

### 用户 steering 子 agent

- 子 agent 运行期间，用户可在侧边栏切入该子 session，像主 agent 一样 **steering**（`POST /api/sessions/{child}/message` 在子 agent 运行时走 steer 路径，直接把引导消息 append 进 history）。子 agent 的 run 循环在 turn 边界（续跑判断时）pickup 未读的 steering 消息。
- 子 agent 是**有界任务**：steering 只是中途引导，不改变"跑完即收尾"的语义。子 agent 最终以一条总结/最终回复结束。

### 中断 -> 总结 -> 退出

用户在子 session 视图点 **停止（■）**，触发 `POST /api/sessions/{child}/interrupt`（子 session 的停止按钮走 interrupt 而非 cancel，见前端）：

1. 后端调用 `child_agent.interrupt()`：置中断标志 `_interrupted`、解除可能挂起的权限等待、**立即 kill 正在运行的 bash 子进程组**，并 **cancel 当前 run task**。cancel 立刻把 `CancelledError` 抛到 run 正在 await 的点--无论是 LLM 流的 `async for chunk in stream` 还是 bash 的 `asyncio.to_thread`，都不会空等。
2. run 的 `except CancelledError` 处理检测到 `_interrupted` 为真，判定这是中断而非主 agent 的取消：调 `task.uncancel()` 清除挂起的取消，**保留被中断 turn 的部分输出**（半截思维链以 `{role: assistant, content: "", reasoning_content: "..."}` 落盘；下个 turn 投影给 API 时 `_assistant_dict(for_api=True)` 把空 content 替换成一个空格 `" "`--因为 glm-5.2 会静默丢弃 content 为空串的 assistant 消息连同其 `reasoning_content`，传空格才能让模型继承半截推理），**抢救在飞 bash 的部分输出**（见下），**补齐 dangling tool_calls**（见下），然后执行**一个总结 turn**：以 user 消息注入"你被中断了。请简明总结你目前的发现/进展，然后停止。"，做一次无工具的 LLM 调用，输出作为子 agent 的最终 assistant 消息。
3. 子 agent 发出 `interrupted` 事件并退出 run（随后 sealed，见下）。
4. 主 agent 的 `task` 工具取这条总结作为该子 agent 的结果（`state="interrupted"`）。

> 与主 agent 取消的区别：主 agent 取消（`cancel()`）也 cancel task，但 `_interrupted` 为假，run 的 `except CancelledError` 走硬停分支（发 `cancelled`、不总结、向子 agent 传播取消）。中断只改 `_interrupted` 这一个标志就分流出"总结退出"的语义。

### 补齐 dangling tool_calls

中断可能落在 tool 执行中途：此时 assistant 消息（带 `tool_calls`）已 append 进 history，但部分 tool result 还没落。OpenAI 兼容 API 会拒绝 `tool_calls` 后面没有对应 `tool` result 的消息，总结 turn 的调用会因此报错。中断/取消处理先调 `_salvage_inflight_tool()` 给在飞的 bash 调用补一条带部分输出 + "command cancelled by user" 标注的 result（见「bash 中断」），再由 `_patch_dangling_tool_calls()` 扫描 history 末尾的 assistant 消息，对任何仍没有对应 result 的 `tool_call_id`（未启动的非 bash 工具、或 bash 无输出时）追加一条 `content="[interrupted]"` 的占位 tool result，让 history 重新自洽。

### bash 中断

工具在 `asyncio.to_thread` 的工作线程里执行（见 [streaming.md](./streaming.md)），bash 用 `subprocess.Popen` 启动命令后用两个读取线程逐行 drain stdout/stderr（非 `subprocess.run` / `communicate`），且 `start_new_session=True` 把命令放进独立进程组。Popen 句柄通过 `on_proc` 回调注册到 agent 的 `_current_proc`。

`interrupt()`（以及主 agent 的 `cancel()`/`stop()`，经统一的 `_kill_current_proc()`）若发现 `_current_proc` 还活着，调 `kill_process_group`（`os.killpg` 整组 SIGKILL）：

- 只 kill shell 不够：`shell=True` 下真正的命令（如 `sleep`）是 shell 的子进程、继承 stdout 管道，shell 死了子进程还活着持管，读取线程的 `readline` 会阻塞到子进程结束。杀整组才能让管道 EOF、读取线程退出、`proc.wait()` 立即返回。
- kill 后工作线程的 `execute_tool` 很快返回（exit code -9）；但中断/取消不等它返回--cancel task 直接让 run 的 `await asyncio.to_thread` 抛 `CancelledError`，立刻进入总结/硬停。工作线程在后台收尾，无害。
- **抢救部分输出**：cancel 掉的 `asyncio.to_thread` 不会把 `execute_tool` 的返回值交还 run，bash 已产出的输出本会丢失。`_make_output_callback` 在流式推送每行给前端的同时，把它们累积进 `_tool_output_buffer`（按 `tool_call_id`）；`_salvage_inflight_tool()` 找到第一个还没 result 的 tool_call（即在飞的那个），若是 bash 就把缓冲里的 stdout/stderr 按 bash 的输出形状（stdout + `[stderr]` 段）拼出、套用 `max_output_lines/max_output_chars` 截断、末尾加 `[command cancelled by user; output above is partial]` 标注，作为该 tool_call 的 result 落盘。这样模型在总结 turn（中断）或下个 turn（硬停取消）能看到命令已产出的内容，而非空洞的 `[interrupted]`。

非 bash 工具（read/edit/write）很快返回，但中断同样靠 cancel task 立刻生效，不等它们。

### 结束即 sealed（不可复用）

子 agent 的 run 一旦结束（正常完成 / 中断总结 / max_steps / 出错），即置 `_sealed = True`。此后 `POST /api/sessions/{child}/message` 一律拒绝（"subagent 已结束，不再接受输入"）。子 session 的 history 已落盘，**长期可在侧边栏只读查看**，但不支持续跑/复用。

### 取消传播

中断（interrupt）和取消（cancel）都靠 `task.cancel()` 落地，都会在 run 里抛 `CancelledError`。两者**唯一**的区别是 `_interrupted` 标志：只有 `interrupt()` 会把它置真，`cancel()` 不碰它。run 的 `except CancelledError` 据此分流--`_interrupted` 真 -> 总结退出；`_interrupted` 假 -> 硬停（发 `cancelled`、不总结、向自己的子 agent 传播取消）。这就是"主 agent 停掉主会话时，子 agent 不做退出总结"的保证：主 agent 对子 agent 调的是 `cancel()`（`_interrupted` 保持假），子 agent 走硬停分支。

- **子 agent 中断**：只影响该子 agent（产出总结后退出）；其它子 agent 不受影响，`gather` 照常等它们完成。
- **主 agent 取消**（用户停掉主会话）：取消 `task` 工具的 `await`（`asyncio.gather` 被取消时会向其内部所有子 task 传播取消），且 `Agent.cancel()` 显式对每个运行中的子 agent 再发一次 `cancel()`。子 agent `_interrupted` 为假 -> 硬停、**不产出总结**。本次 `task` 调用整体作废、不回填结果（`_run_subagents` 来不及取子 agent 的 `_final_state`，因为 `gather` 抛了 `CancelledError`）。

### 边界情况

- **主 agent 取消优先于子 agent 中断**：若某个子 agent 已被用户 interrupt、正在跑总结 turn，此时用户停掉主会话，取消信号会传到该子 agent，打断其总结 turn（`_summarize_and_exit` 内的 await 抛 `CancelledError`，被 `except CancelledError: raise` 透传），子 agent 硬停、**不完成总结**。主 agent 的取消语义始终更强。
- **中断保留被中断 turn 的部分输出**：`CancelledError` 落在 LLM 流的 `async for chunk` 上时，本轮的 `AssistantMessage` 已经 drain 开始时 append 进 history（`persist=False`，未入盘），并已累积部分文本/思维链。interrupt 路径**保留**这个未 persist 的消息（有 thinking/content/tool_calls 时 `save()` 落盘，只有真正空时才 pop），半截思维链以 `{role: assistant, content: "", reasoning_content: "..."}` 进 history。投影给 API 时，无 tool_calls 且 content 为空的 turn 由 `_assistant_dict(for_api=True)` 把 content 替换成空格 `" "`（glm-5.2 会丢弃 content 为空串的 assistant 消息连同 reasoning，传空格才能继承半截推理；前端/存储仍保留真实空串）。若中断落在 tool 执行中途（消息已 persist），则保留，先 `_salvage_inflight_tool` 抢救在飞 bash 的部分输出，再由 `_patch_dangling_tool_calls` 兜底。
- **中断极早期窗口**：`_run_as_subagent` 在 `set_running(True)` 后、进入 `run()` 前还发了一条 user 事件。若中断的 `cancel()` 恰好落在这个 await 上，`CancelledError` 不在 `run()` 的 try 内（还在 `_run_as_subagent` 里），子 agent 直接硬停、无总结。窗口极小（仅一条事件发送），可接受。
- **总结 turn 自身失败**：中断后总结 turn 的 LLM 调用若抛异常（API 错误等），被 `except Exception` 兜住，子 agent 以 `state="error"` 退出（`_final_result` 记录失败原因），不向上抛、不拖垮父 agent 的 `gather`。

### max_steps

子 agent 默认 `max_steps = 100`（一轮 LLM + 其工具调用算一步）。超过即停止并返回 `<task_error>`，作为该子 agent 的结果（`state="error"`）。防失控兜底。

## 六、子 agent 权限请求（交互式、全局可见）

子 agent 的 read/edit/write/bash 企图访问工作区 + 继承的 `additional_dirs` 之外的路径时，**触发交互式权限请求**（沿用主 agent 的 `_permission_event` 机制），而非直接拒绝。

关键要求：**权限提示必须全局可见**--用户当前无论在主 agent session、还是某个兄弟子 session，都能收到提示并审批。提示必须**写明是哪个子 agent、请求访问哪里**。

机制：

- 父 agent 持有其所有运行中子 agent 的列表。子 agent 触发权限请求时，由父 agent **fan-out** 一个 `subagent_permission_request` 事件到：父 agent 自己的 broker + 所有运行中子 agent（含请求者自身）的 broker，这样用户无论订阅哪个会话流都能收到。
- 事件载荷：`{type:"subagent_permission_request", child_session, child_type, child_description, path, tool, message}`。
- 子 agent `await _permission_event`，暂停等待。
- 主 agent 自己的权限请求（`permission_request`）与 plan-exit 请求（`plan_exit_request`）同样 fan-out 到全部运行中子 agent 的 broker；两类事件都带 `session` 字段标明请求方，保证浏览子 session 时也能看到并审批主 agent 的请求。
- 前端把收到的请求统一存入 **pending requests 列表**：同一主会话组（主 session + 其子 agent）内的 pending 请求弹横幅；侧边栏 "Allowed directories" 下方有可展开的 **Pending Requests 列表**，列出并审批全部 pending 请求（目录授权 + 切换到 build 模式）。多个请求并发时互不覆盖、各自独立审批；横幅聚合显示 "N permission requests are pending" 并可跳转到 Pending Requests 列表。
- Approve / Deny 调 `POST /api/sessions/{child}/permission`（plan-exit 走 `/plan_exit`）。Approve：把路径加入该子 agent 的 `additional_dirs`（持久化到子 session.json）并重试该工具；Deny：子 agent 收到拒绝消息，工具返回错误，子 agent 继续。

> v1 批准的目录只加到该子 agent，不自动传播给父或兄弟（权限语义隔离）；但侧边栏 Allowed directories 按主会话组合并展示，浏览组内任意 session 都能看到全部授权目录并标注来源。传播留待后续。

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
<result>...错误信息 / max_steps 超限说明...</result>
</subagent>
</task_result>
```

`state` ∈ {`completed`, `interrupted`, `error`}。`session` 是子 session 名，前端据此跳转。

## 八、Session 结构与注册

### 目录结构（扁平）

Session 目录名是一个稳定的 UUID 标识（`id`），人可读的名字存在 `session.json` 的 `name` 字段里，可随时改名而不移动目录。所有 `/api/sessions/{id}/...` endpoint 用这个 `id`（即目录名）寻址。

```
sessions/
  <session_id>/               # 主 session（UUID 目录）
    session.json              # name, working_dir, plan_mode, additional_dirs, created_at, session_id, titled
    history.json
  <child_id>/                 # 子 session（UUID 目录，与父平级）
    session.json              # parent_session, subagent_type, depth, description, additional_dirs, created_at
    history.json
    agent.log
```

子 session 的 `session.json` 记录 `parent_session`（父 session 的 `id`/UUID）、`subagent_type`、`depth`、`description`、`additional_dirs`、`created_at`（创建时间戳），用于前端标题、树归属、侧栏排序与权限目录持久化。扁平结构与现有 session 模型兼容。主 session 的 `session.json` 同样写入 `created_at`。`GET /api/sessions` 列表里每项带 `id`（目录名）与 `name`（人可读名）；改名走 `POST /api/sessions/{id}/rename`（只改 `name`，目录不动）。旧 session（目录名即人可读名）向后兼容：其 `id` 等于目录名。

**会话自动命名**：主 session 创建时 `name` 默认取工作目录 basename。用户发出第一条消息时，`Agent.start` 取该消息的前 50 个字符（空白折叠为单空格）写回 `name`，并在 `session.json` 置 `titled: true`。`titled` 一旦为真就不再自动改名——既保证 revert 回退到首条消息重跑时幂等，也确保用户提前手动 rename（rename endpoint 同样置 `titled: true`）的选择不被覆盖。子 session 不参与自动命名（用 `description` 展示）。前端通过既有的 3 秒 `GET /api/sessions` 轮询感知 `name` 变化，无需额外事件。

### 运行期注册

子 Agent 必须能被前端通过 `/stream`、`/history`、`/message`（steer）、`/interrupt`、`/permission` 等 endpoint 访问。这些 endpoint 都走 server 的 `agents: dict[str, Agent]`。为避免 `agent.py` 反向依赖 `server.py`：

- `Agent.__init__` 增加可选参数 `registry: dict[str, Agent] | None`。server 创建主 agent 时把自己的 `agents` 字典传入；主 agent 创建子 agent 时把同一个 `registry` 传入，并在创建后 `registry[child_name] = child`。
- 这样子 agent 自动进入 server 的 `agents` 字典，现有 endpoint（按 name 查 `agents`）对子 session 直接可用。
- v1 为前台内存态：服务重启则运行中的子 agent 实例丢失（主 agent 同理），可接受。子 session 的 history 已落盘，重启后仍可只读回看（但实例不在 `agents` 字典时，按需从磁盘重建只读视图）。

## 九、Streaming 与前端

### 事件

主 agent 的 broker（父时间线）在 `task` 工具期间发：

| 事件 | 时机 | 载荷 |
|---|---|---|
| `tool_start` | `task` 开始 | `{tool:"task", args:{tasks:[...]}}` |
| `subagents` | 子 agent 全部 spawn 后 | `{parent, children:[{session, type, description, state:"running"}]}` |
| `subagent_state` | 某子 agent 结束时即发（逐个增量，不等其它兄弟） | `{session, state}` |
| `subagent_permission_request` | 子 agent 请求权限时（fan-out，见第六节） | `{child_session, child_type, child_description, path, tool, message}` |
| `tool_result` | `gather` 完成 | `{tool:"task", result:"<task_result>...</task_result>"}` |

子 agent 的 broker 只发到自己 session 的 stream（`turn`/`thinking`/`text`/`tool_start`/`tool_result`/`done`/`interrupted`），**不**灌进父时间线。前端点开子 session 时订阅 `/api/sessions/{child}/stream`。

### 前端树状展示

- **侧边栏树**：父 session 节点下展开挂子 session 节点（带 type/description/状态徽标）。点击切换到该会话视图。子 session 完成后仍留在树里，只读可查。子节点按 `created_at` **降序**排列（新 spawn 的排顶部，老的在下），缺少时间戳的遗留 session 排末尾。角色徽标用 `subagent_type` 前两字符（`EX`/`GE`）：explore 保持灰底、字体用浅黄（#d29922，与 plan mode 同色系，呼应其只读语义），general 维持默认浅蓝。
- **子 session 视图**：复用 `ChatView`。
  - 运行中：**显示输入框**（用于 steering）+ **■ 停止按钮**（与主 session 同位，点击走 `interrupt`：硬停当前工作后总结退出）；订阅子 session SSE。
  - 已结束（sealed）：**禁用输入框**（提示"该 subagent 已结束"），仅展示历史，可返回父会话。停止按钮随之隐藏。
  - 顶部 subagent-bar：显示角色标签、描述、返回父会话导航；sealed 后显示"finished (read-only)"。
- **权限横幅**：`subagent_permission_request` 事件触发全局横幅（不论当前在哪个会话视图），写明子 agent 身份与请求路径，Approve/Deny。
- `store.ts`：处理 `subagents` / `subagent_state` / `subagent_permission_request` 事件；维护会话树与各子 session 的 `isStreaming` / `sealed` 状态。`subagent_state` 在 `_run_subagents` 里由每个子 agent 的运行 wrapper 在其 `run()` 返回后**立即**发出（而非等 `gather` 全部结束再批量发），故 task 泡泡里每个子 agent 行随其完成逐个翻为终态。主会话收到 `done` / `cancelled` / `interrupted` 时，后端对运行中子 agent 的硬停不会发出 `subagent_state`（取消时 wrapper 的 `await c._run_as_subagent()` 抛 `CancelledError`，其后的发出语句不执行），故前端在此刻主动把仍 `running` 的子会话标记为停止（task 节点 state 置为 `completed`/`interrupted`、侧栏 `is_running` 置假），让侧栏即时反映中断而不必等下一次 session 轮询。

## 十、可选增强（待你拍板是否纳入 v1）

以下来自讨论稿，**默认不纳入**：

1. **summaryPolicy（结果蒸馏兜底）**：子 agent 跑完取最后一条文本后，若过短（如 < 150 字符），追加一条 user 消息让其扩写，最多重试 1 次。低成本高收益，但会增加子 agent 的 turn/token。
2. **wall-clock 超时**：子 agent run 加超时（如 10 分钟），超时返回错误。
3. **并行上限**：`gather` 加 `Semaphore` 限制同时运行的子 agent 数（如 ≤ 4），超了排队。

> `max_steps`（默认 100）已纳入 v1，不在此列。

## 十一、实现拆解（审核通过后执行）

1. **`permission.py`**：`BuildModePermission` / `PlanModePermission` 的 `tool_names` 增加 `task`；新增 `TASK_TOOL_DEF` 工具定义常量（或 `TaskTool` 子类），由这两个 permission 在 `filter_definitions` 里拼上。Explore/General 不变（天然不含 task）。
2. **`agent.py`**：
   - `__init__` 加 `registry`、`depth`、`parent_broker`、`max_steps`、`_sealed` 参数/字段。
   - 工具派发处加 `elif tool_name == "task": tool_result = await self._run_subagents(args)`，作为 `intercept` 之后、`exit_plan_mode` 之后的分支。
   - 新增 `_run_subagents(args)`：校验（含派生权限：plan 仅 explore）、创建子 Agent（注入 prompt、permission、depth、registry、parent_broker、max_steps）、`gather`、组装 `<task_result>`。
   - run 循环加 `max_steps` 计数与超限退出。
   - 新增 `interrupt()`：置 `_interrupted` 标志 + kill bash 进程组 + **cancel 当前 run task**（立刻中断 LLM 流/工具）；run 的 `except CancelledError` 检测 `_interrupted` 为真则 `uncancel` + 保留半截思维链 + 抢救在飞 bash 部分输出 + 补齐 dangling tool_calls + 做总结 turn，结束置 `_sealed`。`_interrupted` 为假（主 agent 取消）则硬停不总结（同样保留半截输出 + 抢救 bash）。
   - 子 agent 权限请求：复用 `_permission_event`；触发时经 `parent_broker` 由父 agent fan-out `subagent_permission_request` 到父 + 所有兄弟 broker。
   - run 结束（任何原因）置 `_sealed = True`。
   - 主 agent 取消时传播取消给运行中的子 agent（硬停）。
3. **`server.py`**：创建主 agent 时传 `registry=agents`；`/message` 对 sealed 子 agent 拒绝；新增 `POST /api/sessions/{id}/interrupt`（调 `agent.interrupt()`）；`/stream`、`/history`、`/permission` 对子 session 复用现有逻辑；侧边栏 session 列表返回树结构（带 parent/children）。
4. **前端**：`store.ts` 会话树 + `subagents`/`subagent_state`/`subagent_permission_request` 事件处理；侧边栏树组件；`ToolEntry`（或新 `SubagentTree` 组件）渲染 `task` 节点；子 session 视图（复用 `ChatView`、运行中显示输入+■ 停止按钮（走 interrupt）、sealed 禁用输入、返回导航）；全局权限横幅。`ChatInput.stop()` 按 `isSubagent` 分流：subagent -> `/interrupt`，主 agent -> `/cancel`。
5. **提示词**：提示词全部硬编码在 `src/trilobite/prompts.py`（不可配置）：`SYSTEM_PROMPT`（含 `task` 工具使用指引：subagent 屏蔽上下文、省主 agent token，是非 needle 查询的探索/上下文收集的**首选**方式，可在单次调用里并行多个独立子任务；needle 查询--已知文件路径、单个定义、2-3 个已知文件--直接用 read/grep 不开 subagent；开 subagent 要给自包含 prompt；子 agent 输出对用户不可见，主 agent 需转述）、`SUBAGENT_ROLE_PREFIX`（公共前缀）、`SUBAGENT_ROLE_PROMPTS`（explore/general 角色提示）。`tool_call.py` 的 `TASK_TOOL_DEF` 描述采用同一正向框架（强调省 token + 探索首选，排除项收窄为 needle 查询）。

## 十二、风险与决策记录

- **一次调用 fan-out vs 多次调用 gather**：选"一次 `task` 调用、`tasks` 数组 fan-out"（单次调用产出多个并行子 agent）。备选是"主 agent 一轮里发多个单子 agent 的 `task` 调用、由 run 循环 gather"（opencode 风格）。选前者是因为：单一 gather 点、单一中断句柄、结果按批结构化返回，且不必改造 run 循环去特判"同轮多 task 调用"。
- **task 工具异步化范围**：只让 `task` 异步，不把 `execute_tool` 整体异步化，控制改动面。
- **子 agent 权限请求**：交互式 + 全局广播（父 agent fan-out 到父及所有兄弟 broker），而非越界即拒；提示写明子 agent 身份与路径。
- **plan 模式派生**：仅 explore（只读），禁 general，保住只读语义。
- **steering 与 sealed**：用户可 steer 运行中的子 agent（有界任务的中途引导）；run 一旦结束即 sealed，不可复用，但历史长期只读可查。
- **max_steps=100**：纳入 v1 作防失控兜底。
- **提示词正向框架**：系统提示词和 `task` 工具描述采用正向框架--把 subagent 定位为"屏蔽上下文、省 token、探索首选"，并给出正面用例（如"错误在哪处理""代码库结构"），排除项收窄为 needle 查询（已知文件路径/单个定义/2-3 个已知文件）。早期版本用"不要为单个 read/bash 能做的事开 subagent""spawning costs a full independent run"这类宽泛否定且在提示词与工具描述里各重复一遍，对 DeepSeek 这类对否定指令敏感的模型造成双倍抑制，使模型几乎从不主动 spawn subagent。对照 opencode 的 `task.txt` 与各模型 `prompt/*.txt`（"prefer to use the Task tool to reduce context usage""proactively use"），正向框架是其模型乐于派生 subagent 的主因。
