# Subagents 讨论稿（基于 opencode 调研）

> 状态：讨论稿，待评审。作者基于 opencode 源码（`packages/opencode/src/tool/task.ts`、`packages/core/src/agent.ts`、`packages/core/src/plugin/agent.ts`、`packages/opencode/src/agent/subagent-permissions.ts` 等）整理。

## 一、opencode 的 subagent 是什么

Subagent 是 opencode 里"主 agent 通过一个 tool call 派生出来的子 agent"。主 agent 在对话中调用 `task` 工具，传入 `prompt` + `subagent_type`，opencode 就 **新建一个子 session**，用指定 agent 定义（system prompt / 权限 / 模型）跑一遍，结束后把子 agent 的最终文本回填给主 agent。对用户来说，子 session 是一条可以点进去看的独立会话；对主 agent 来说，它只拿到一段 `<task_result>...</task_result>` 文本。

内置 agent（见 `packages/core/src/plugin/agent.ts`）有这些：

| agent | mode | 作用 |
|---|---|---|
| `build` | primary | 默认主 agent，全权限 |
| `plan` | primary | 只读规划模式 |
| `general` | **subagent** | 通用子 agent，研究复杂问题、多步任务，可并行 |
| `explore` | **subagent** | 只读代码探索专家（grep/glob/read/webfetch），prompt 强调成"文件搜索专家" |
| `compaction` / `title` / `summary` | primary, hidden | 内部隐藏 agent，分别做压缩、起标题、生成摘要 |

关键区分是 `mode` 字段：`primary`（可作主 agent）、`subagent`（只能被 task 工具调用）、`all`（两者皆可）。`hidden` 的 agent 不出现在 `@` 补全菜单里。用户也可以在 `opencode.json` 的 `agent` 字段自定义 agent（指定 model / prompt / mode / permission / steps 等）。

## 二、核心功能清单

1. **`task` 工具派生**：主 agent 调用 `task(prompt, subagent_type, description)`，opencode 校验该 agent 类型存在 → 新建子 session → 用子 agent 的配置跑一轮完整 agentic loop → 取子 session 最后一条 assistant 文本作为结果。
2. **父子 session 关系**：子 session 带 `parentID`，标题形如 `<description> (@general subagent)`。前端可以从父 session 的 task tool 卡片点进去看子 session（`session-route.ts` 的 `rootSession` 沿 `parentID` 上溯）。子 session 不出现在左侧 session 列表里（缓命中才显示），有专门的 not-found fallback。
3. **权限派生**（`subagent-permissions.ts`）：子 session 的权限 = 父 session 的 deny 规则 + external_directory 规则，再叠加子 agent 自身定义的 permission。默认还强制 deny `todowrite` 和 `task`（子 agent 不能再开子 agent，除非自己显式放行）。即 **父的限制向下传递，子的能力由子自己定义**。
4. **嵌套深度限制**：`subagent_depth` 配置项（默认 1）。task 工具执行时沿 `parentID` 数深度，超过就报错，提示用户改配置。默认 1 意味着 subagent 不能再派生 subagent。
5. **前台 / 后台两种模式**（实验特性，需 `OPENCODE_EXPERIMENTAL_BACKGROUND_SUBAGENTS=true`）：
   - 前台（默认）：task 工具阻塞等待子 agent 跑完，结果直接作为 tool result 返回。主 agent 可在同一条消息里发多个 task tool call **并行**跑多个子 agent。
   - 后台：`background=true` 立即返回"已启动"，主 agent 继续干别的活；子 agent 跑完后，opencode 往 **父 session 注入一条 synthetic user 消息**（`<task ... state="completed"><task_result>...</task_result></task>`）通知主 agent。期间主 agent 被叮嘱"不要 sleep / 不要 poll、别和子 agent 抢同一批文件"。
   - 同一个 `task_id` 可以"续跑"：传 task_id 复用已有子 session，带着之前的消息继续，而不是开全新上下文。
6. **`@mention` 入口**：用户在输入框打 `@general` / `@explore` 可以直接把消息路由给子 agent（`session/prompt.ts` 里把 mention 转成一次 task 调用）。这是除了"主 agent 自主决定调用 task"之外的第二入口。
7. **隔离的上下文**：每次 task 调用默认全新上下文（只带主 agent 给的 prompt），主 agent 的历史不进子 agent。这也正是 subagent 的价值--**省主 agent 的 token**，把探索/搜索这种吃上下文的活外包出去，只回收结论。
8. **模型可独立配置**：每个 agent 可指定自己的 `model`（如 explore 用便宜模型），不指定则继承父 agent 当前模型。

## 三、实现机制拆解（对我们有参考价值的细节）

### task 工具的参数与结果格式

```
参数: { description(3-5词), prompt(详细任务), subagent_type, task_id?(续跑), background?, command? }
结果: <task id="ses_xxx" state="completed"><task_result>子agent最后一条文本</task_result></task>
```

工具描述（`task.txt`）反复强调：能直接 grep/glob/read 的就别开 subagent；开 subagent 要给"高度详细的任务描述 + 明确要求返回什么"；子 agent 输出对用户不可见，主 agent 要转述给用户。这套提示词工程是让 LLM 正确使用 subagent 的关键。

### 权限合并的实际效果

- `explore` 子 agent：`*` 全 deny，只 allow `grep/glob/list/bash/webfetch/websearch/read`。是个真正的只读探子。
- `general` 子 agent：继承默认全权限，但 deny `todowrite`（子 agent 不维护 TODO 列表）。
- 子 agent 默认 deny `task` → 防止无限套娃，配合 `subagent_depth` 双保险。

### 后台通知的注入机制

后台 task 完成后，opencode 不靠轮询，而是往父 session **追加一条 synthetic user 消息**触发主 agent 继续。这个设计很轻：父 agent 的下一次 run 自然会读到这条消息并据此行动。前提是父 agent 处于可继续状态（idle），否则要排队。

### 前端展示

父 session 时间线里，task tool call 渲染成一张可点击卡片（`message-timeline.tsx` 的 `taskDescription`），点击跳到子 session 视图。子 session 视图标题取自 task 的 `description`，并有"返回父 session"的导航。TUI 端（`subagent-data.ts`）单独维护一份子 session 的精简数据流（有 commit/role/error 等条数上限），避免把子 agent 的全部输出灌进主时间线。

## 四、Trilobite 现状对照

我们当前（`src/trilobite/`）：

- **没有 subagent 概念**。只有 plan/build 双模式（`agent.py` 的 `_plan_mode`），靠 `exit_plan_mode` virtual tool 切换。
- **工具是全局的**（`tool_call.py` 的 `ALL_TOOLS`），所有 agent 共享同一套工具，无 per-agent 权限。plan 模式靠运行时拦截 `write` 实现，不是声明式权限。
- **Agent 类是单例式**：一个 session 一个 `Agent`，持有 `History`、`StreamBroker`、`session_dir`。没有 parent/child session 概念，`session.json` 只存 `additional_dirs` / `token_count`。
- **streaming 是 per-session broker**（`broker.py`），事件 `{type, ...}` 直连前端。没有跨 session 的事件关联。
- **压缩**已经有一套（`compaction.py`，kimi 风格 handoff），这其实和 opencode 的 `compaction` hidden agent 思路一致--我们用专用 prompt，他们用专用 agent。可保留。
- **配置**（`config_example/config.yaml`）只有 api_key / model / max_tokens / compaction 触发比例，没有 agent 定义。

差距主要在三块：① agent 定义模型（声明式 agent + 权限）；② task 工具 + 父子 session；③ 前端子 session 导航。

## 五、我们该怎么抄（建议分阶段）

### 阶段 0：先想清楚要不要做、做到哪

subagent 的真实收益是 **省主 agent 上下文 + 并行**。我们用 DeepSeek，上下文窗口和成本敏感度跟 opencode（多用 Claude）不同，但"探索类任务吃上下文"的痛点一样存在。建议 **先做最小可用版**（阶段 1+2），后台模式和自定义 agent 留到后面。

### 阶段 1：声明式 agent 定义 + per-agent 权限

- 在 `config.yaml` 增加 `agents` 段，每个 agent 定义 `{name, description, system_prompt(可选), mode, tools(允许列表), model(可选)}`。
- 内置三个：`build`（primary，全工具）、`plan`（primary，只读）、`explore`（subagent，只 read/bash/grep-glob--我们暂时没有 grep/glob 工具，可先用 read+bash 替代）。
- `tool_call.py` 改造：`get_tool_definitions()` 和 `execute_tool()` 接受 `agent` 参数，按 agent 的 `tools` 白名单过滤。plan 模式的 write 拦截改成"plan agent 的 tools 不含 write"，统一机制。
- 这一步不引入 subagent，但把"agent = 权限+prompt 的声明式单元"立起来，是后续基础。同时也顺手把 plan/build 模式收敛进同一套 agent 模型。

### 阶段 2：task 工具 + 前台子 session

这是 subagent 的核心。建议实现：

- **新增 `tools/task.py`**：参数 `{description, prompt, subagent_type}`。执行时：
  1. 校验 `subagent_type` 是 mode=subagent 的 agent；
  2. 在 `session_dir` 下新建子目录（子 session），`session.json` 记 `parent_session_id` / `parent_dir` / `agent`；
  3. **新建一个子 `Agent` 实例**（复用 `Agent.__init__`，传子 session_dir + 子 agent 配置），子 agent 的 `working_dir` 与父一致；
  4. 把 `prompt` 作为子 agent 的第一条 user 消息，`await child_agent.run()` 跑完；
  5. 取子 agent history 最后一条 assistant 文本，包装成 `<task_result>...</task_result>` 返回给父 agent 的 tool result。
- **嵌套深度**：`Agent` 持有 `depth`，task 工具检查 `depth >= max_depth`（默认 1）则拒绝。子 agent 的 `depth = parent.depth + 1`。
- **权限派生**：子 agent 用自身 agent 定义的 tools 白名单即可（我们暂无 deny/ask 细粒度权限，白名单等价于"只允许这些工具"）。父的 `additional_dirs` 透传给子。
- **关键约束**：子 agent **不能**再调用 task（白名单不含 task），实现套娃防护。子 agent 不维护 TODO（白名单不含 todo）。
- **streaming**：子 agent 自己有 broker，事件发到子 session 的 stream。**父 session 的 stream 里只发一个 `tool` 事件**（task 工具调用 + 结果），不把子 agent 的中间过程灌进父时间线。前端通过 tool 事件里的 `subagent_session_id` 跳转查看子 session。

这一步要解决的工程难点：
- `Agent.run()` 当前是"跑到没有 tool call 为止"的自循环。子 agent 复用没问题，但要确保子 agent 的 `exit_plan_mode` 等 virtual tool 不该出现在 subagent 的工具列表里（subagent 不该切模式）。
- 子 agent 跑完如何"取最后一条 assistant 文本"：直接读 `child.history.raw` 找最后一条 role=assistant。
- 子 agent 的取消：父 agent 被 steering/取消时，要能级联取消子 agent。`Agent._task` 需要支持嵌套取消。

### 阶段 3：前端子 session 导航

- `store.ts` 增加 session 树概念：一个 task tool 事件携带 `subagent_session_id`，点击在右侧开一个子 session 视图（或新 tab）。
- `ToolEntry` 组件里 task 类工具渲染成可点击卡片："explore 子任务：查找 API 入口 → 已完成/进行中"。
- 子 session 视图复用 `ChatView`，但隐藏输入框（子 agent 不接受用户直接输入，除非阶段 4 的续跑）。
- SSE 订阅：子 session 也有自己的 `/stream?session_id=...`，前端切过去时订阅子 session 的 broker。

### 阶段 4（可选，后置）：后台模式 + 续跑 + @mention

- **后台模式**：需要把"父 agent 空闲时被注入 synthetic 消息继续"做出来。我们当前 `POST /message` 是用户驱动的转向；要支持"系统注入一条 user 消息并触发 run"，相当于给父 agent 加一个内部 steering 入口。复杂度不低，建议后置。
- **续跑（task_id）**：task 工具传已有子 session id，复用其 history 继续。实现不难（不新建子 Agent，而是 load 已有的），但要处理子 agent 的并发（同一子 session 不能同时跑两次）。
- **`@mention`**：前端输入框解析 `@explore`，`POST /message` 时带上 `agent` 字段，后端直接用该 agent 跑（不开父 agent，相当于把当前消息直接交给 subagent）。这是"用户显式指挥 subagent"的入口，比后台模式简单，可优先于后台模式做。

## 六、设计决策与风险

1. **子 agent 复用 `Agent` 类 vs 新写轻量 runner**：建议复用。`Agent` 已经处理了 streaming / tool loop / compaction / history，子 agent 本质就是"换个 system prompt 和工具集的独立 run"。代价是 `Agent` 要去掉一些"主 session 专用"的假设（如 `exit_plan_mode`、用户 steering），把这些做成可选。可用一个 `is_subagent` 标志或子类化。

2. **权限模型要不要抄 opencode 的 deny/ask/allow 三态 ruleset**：**建议暂不**。我们的 plan/build 模式 + 工具白名单已能覆盖 subagent 需求（explore 只读、general 全能）。opencode 的细粒度 permission 主要服务于它复杂的 ask 交互和 external_directory。我们等真有需求再引入，避免过度设计。

3. **上下文隔离的边界**：子 agent 只拿到 prompt，不拿父 history。这是 subagent 省 token 的关键，必须坚持。但要注意：主 agent 给子 agent 的 prompt 必须 **自包含**（文件路径、目标、返回格式都要写清），否则子 agent 会瞎找。这要靠 system prompt 里的 task 工具使用指引来约束（抄 `task.txt` 的措辞）。

4. **DeepSeek 的并行能力**：opencode 鼓励"一条消息多个 task call 并行"。我们若要并行，需要在 `agent.py` 的 tool 执行段把多个 task 工具调用 `asyncio.gather`。当前 `execute_tool` 是同步的，task 工具会是第一个异步工具，需要改造 tool 执行路径支持 async（`bash` 工具其实也该是 async，可顺势统一）。

5. **失败/超时/取消**：子 agent 跑飞了（死循环 tool call）怎么办？靠 `steps` 上限（opencode 有，我们没有）。建议给每个 agent 加 `max_steps`，子 agent 尤其要设小（如 explore 限 20 步）。子 agent 报错时，task 工具返回 `<task_error>` 文本而非抛异常，让父 agent 能消化。

6. **session 目录结构**：建议 `sessions/<parent_id>/<child_id>/` 嵌套，或扁平 `sessions/<id>/` + `session.json` 里记 parent。后者更简单、与现有结构兼容，推荐。

## 七、建议的最小落地顺序

1. **阶段 1**（声明式 agent + per-agent 工具白名单）：重构 `tool_call.py`，`config.yaml` 加 `agents`，把 plan/build 收敛进去。纯重构，无新功能，可独立验证。
2. **阶段 2**（task 工具 + 前台子 session）：新增 `tools/task.py`，`Agent` 支持 `depth` 和子实例化，`execute_tool` 支持 async。带 1~2 个内置 subagent（explore 优先）。
3. **阶段 3**（前端导航）：`ToolEntry` 卡片 + 子 session 视图 + SSE 切换。
4. **阶段 4**（@mention > 续跑 > 后台模式）：按需。

阶段 1+2 落地后，我们就有了"主 agent 把探索外包给 explore 子 agent、只回收结论"的能力，这是 subagent 80% 的价值。后台模式和细粒度权限是长尾，可以观察实际使用后再决定。

---

## 附：opencode 关键文件索引

- `packages/opencode/src/tool/task.ts` -- task 工具主体（前后台、深度、续跑、权限派生调用）
- `packages/opencode/src/tool/task.txt` -- task 工具描述（提示词工程，值得抄）
- `packages/opencode/src/agent/subagent-permissions.ts` -- 权限派生逻辑
- `packages/core/src/plugin/agent.ts` -- 内置 agent 定义（build/plan/general/explore/compaction/title/summary）
- `packages/core/src/agent.ts` -- agent 服务（selectable 过滤掉 subagent/hidden）
- `packages/core/src/v1/config/agent.ts` -- agent 配置 schema（mode/hidden/steps/permission 等）
- `packages/core/src/v1/config/config.ts` -- `subagent_depth` 配置
- `packages/app/src/utils/session-route.ts` -- 父子 session 上溯
- `packages/app/src/pages/session/timeline/message-timeline.tsx` -- task 卡片渲染 + 子 session 标题
- `packages/opencode/src/session/prompt.ts` -- @mention → task 调用转换
