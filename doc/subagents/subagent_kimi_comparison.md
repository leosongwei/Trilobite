# Subagents 对比讨论：kimi-code vs opencode（及对我们的启示）

> 状态：讨论稿，待评审。是 `doc/subagent_discussion.md`（opencode 调研）的姊妹篇。本文基于 kimi-code `packages/agent-core-v2/` 源码整理。

## 一、kimi-code 的 subagent 是什么

和 opencode 思路一致：主 agent 调 `Agent` 工具派生子 agent，子 agent 用独立上下文跑完，只把"总结"回填给主 agent。但 kimi-code 的实现**显著更重、更工程化**，多了好几层 opencode 没有的机制。

核心抽象是 **profile**（不是 opencode 的 "agent + mode"）。profile 是注册式的（`registerAgentProfile`），每个 profile 自带 `{name, description, whenToUse, tools[], systemPrompt 渲染器, summaryPolicy, promptPrefix}`。内置 profile（`profiles.ts` + `plan/profile/plan.ts`）：

| profile | 角色 | tools |
|---|---|---|
| `agent` | 默认主 agent | 全套（含 Agent/AgentSwarm） |
| `coder` | 通用子 agent，**唯一能改代码的 subagent** | 全套（含 Agent/AgentSwarm，可套娃） |
| `explore` | 只读代码探索 | Read/Glob/Grep/Bash(只读)/WebSearch/FetchURL |
| `plan` | 只读规划 | Read/Glob/Grep/WebSearch/FetchURL |

注意：kimi-code **没有** opencode 那个 `mode: primary/subagent/all` 字段。谁是主谁是子，靠"tools 里含不含 `Agent` 工具"隐式区分--主 agent 的 profile 有 Agent 工具能派生，explore/plan 没有。`coder` 也有 Agent 工具，所以 coder 子 agent 可以再派生子 agent（**无显式深度限制**，这点和 opencode 不同）。

## 二、kimi-code 比 opencode 多出来的关键机制

### 1. 结果蒸馏质量控制（summaryPolicy）⭐ 最值得抄

`runAgentTurn.ts` 的 `distillSummary`：子 agent turn 跑完后，取最后一条 assistant 文本作为 summary。如果该 summary **太短**（< `minChars`，默认 200 字符），自动再发一条续写 prompt（`summary-continuation.md`："你上一条回复太简短，请补充技术细节、发现、分析"）让子 agent 扩写，最多重试 `retries` 次。

```ts
const DEFAULT_SUMMARY_POLICY = { minChars: 200, continuationPrompt: SUMMARY_CONTINUATION_PROMPT, retries: 1 }
```

opencode 没有这个--它直接取最后一条文本。kimi-code 的逻辑是：子 agent 的总结是父 agent **唯一**能看到的东西，太短就等于白跑，值得多花一个 turn 兜底。这个机制**实现简单、收益明显**，强烈建议抄。

### 2. 通用子 agent 角色前缀（TASK_AGENT_ROLE_PREFIX）⭐

`profile-shared.ts` 定义一个公共前缀，每个 subagent profile 的 systemPrompt 都拼上它：

> "You are now running as a subagent. All the `user` messages are sent by the main agent. The main agent cannot see your context, it can only see your last message when you finish the task. You must treat the parent agent as your caller. Do not directly ask the end user questions..."

opencode 是在每个 profile 各自的 prompt 里零散写这些约束。kimi-code 抽成公共前缀，统一且不漏。低成本，建议抄。

### 3. AgentSwarm 批量并行工具

opencode 的并行是"一条消息里发多个 `task` tool call"。kimi-code 做成**专用工具 `AgentSwarm`**：参数 `{prompt_template, items[], subagent_type?, resume_agent_ids?}`，模板里 `{{item}}` 占位符被每个 item 替换，一次启动最多 128 个子 agent。

- 有**调度器**（`AgentRunBatch`）：`maxConcurrency` 并发上限，超了排队。
- 有 **rate-limit 挂起**：子 agent 撞限流时发 `subagent.suspended` 事件，任务重新排队等重试（不是直接失败）。
- 有 **enter/exit reminder**：调用 swarm 前后给主 agent 注入"你进入了 swarm 模式 / swarm 模式结束"提示，引导主 agent 先探索再分解任务、不要自己抢着干。
- `resume_agent_ids` 可以在 swarm 里混入"续跑已有子 agent"。

这套比 opencode 的"多个 task call"强大很多（调度、限流重试、批量续跑），但也重很多。

### 4. 成熟的后台任务系统

opencode 的后台是实验特性。kimi-code 的后台任务是**一等公民**：

- 三个配套工具 `TaskList` / `TaskOutput` / `TaskStop`：让 LLM 能列出/读取输出/停止后台任务。后台 subagent 必须在这三个工具启用时才可用（`canRunInBackground` 检查）。
- 完成通知：后台任务终止时，渲染 `<notification ...>` XML（`notificationXml.ts`），通过 `loop.enqueue(TaskNotificationStepRequest)` 注入父 agent 上下文，admission 是 `activeOrNewTurn`--**父 agent 正在跑就折进当前 turn，空闲就开新 turn**。opencode 是注入 synthetic user 消息，思路相同但 kimi-code 有 admission 策略。
- **持久化跨重启**：任务生命周期用 wire journal（`task.started` / `task.terminated` ops）记录，重启后重建"ghost"任务，未投递的通知重放。这是 opencode 没有的。
- 超时：每个 subagent run 默认 2 小时（`subagent.timeout_ms` / `KIMI_SUBAGENT_TIMEOUT_MS`），超时给 resume 提示。

### 5. resume 语义（agent_id vs task_id）

kimi-code 的续跑用 `resume=<agent_id>`（复用 agent 实例，保留全部上下文），和 `subagent_type` 互斥（传了 resume 就不能传 type， resumed agent 保留自己的 type）。opencode 用 `task_id` 复用子 session。语义上 kimi-code 更清晰：resume 的是"那个 agent 实例"，不是"那次任务调用"。对我们的映射上区别不大（我们的 session ≈ agent 实例），但 kimi-code 还多了"resume 时把子 agent 的模型重新对齐到父当前模型"（`realignChildModel`）这种细节。

### 6. promptPrefix（explore 自动注入 git context）

explore profile 有个 `promptPrefix`：启动时调 `collectGitContext(runner, cwd)` 收集仓库 git 状态（branch / 最近 commit / dirty 文件），拼到子 agent 的 prompt 前面。这样 explore 子 agent 一上来就知道仓库当前状态，不用自己再跑 git 命令。opencode 没有这个。中成本、有针对性，值得考虑。

### 7. mirrorAgentRun（调用方侧镜像）

`mirrorAgentRun.ts`：子 agent 的 run 本身不发任何东西到父的 record stream（`runAgentTurn` 是"纯函数，无观察者语义"）。想要在父侧展示，由**调用方**（Agent 工具 / swarm）显式 mirror：把子的 run 镜像到父的 record stream，发 `subagent.spawned/started/completed/failed/suspended` 事件，UI 据此把子 transcript 嵌套在发起 tool call 下面。这是"展示职责归调用方"的清晰分离。opencode 是子 session 独立展示、靠 parentID 关联，没这套镜像事件。

## 三、两者的架构哲学差异（对我们很重要）

| | opencode | kimi-code |
|---|---|---|
| 技术栈 | TypeScript + Effect | TypeScript + 自研 DI/scope/wire-journal |
| 复杂度 | 中（Effect 服务层） | **高**（6 层 domain、wire 回放、Model/Op、跨重启 ghost） |
| 抽象 | agent(mode) + permission ruleset | profile(tools) + task manager + swarm scheduler |
| 持久化 | session JSON + 数据库 | wire journal（每 agent 一个物理日志，可回放重建） |

**对我们的核心判断**：两个都是远比我们复杂的工程实现。我们要抄的是**特性**，不是**架构**。kimi-code 的 DI/scope/wire-journal 那套对我们（Python/FastAPI + 单文件 history.json）是过度工程，绝不能照搬。但它的几个**特性机制**比 opencode 想得更细，值得单独拎出来抄。

## 四、特性级对比表

| 维度 | opencode | kimi-code | 我们该取谁 |
|---|---|---|---|
| 派生工具 | `task` | `Agent` | 名字无所谓，参数结构两者接近 |
| 内置 subagent | general / explore | coder / explore / plan | 抄 kimi 的 coder+explore 划分（coder 能改码、explore 只读）更实用 |
| 并行 | 多个 task call | AgentSwarm 专用工具 | **先抄 opencode 简单并行**（asyncio.gather 多 task 调用）；swarm 工具作为可选增强 |
| 结果质量控制 | 无 | summaryPolicy（minChars+续写+retries）⭐ | **抄 kimi-code**，低成本高收益 |
| 子 agent 角色提示 | 各 profile 零散写 | TASK_AGENT_ROLE_PREFIX 公共前缀 ⭐ | **抄 kimi-code**，统一 |
| 续跑 | task_id | resume=agent_id + 模型重对齐 | 语义借鉴 kimi，实现取简 |
| 后台任务 | 实验特性 | 成熟（TaskList/Output/Stop + notification + 跨重启） | **暂不做**，太重；需要时再做 |
| 超时 | 无 | 2h 可配置 | **抄 kimi-code**（加 wall-clock 超时） |
| 深度限制 | subagent_depth=1 | 无显式（靠 tools 白名单，coder 可套娃） | **两者结合**：白名单（子不含 task）+ depth counter 双保险（沿用 opencode 方案） |
| 权限 | deny/ask/allow 三态 ruleset | tools 数组白名单 + 继承 | **抄 kimi-code 的白名单**（我们无 ask 交互，白名单够用） |
| 上下文注入 | 无 | explore 的 promptPrefix（git context） | 可选抄，中成本 |
| 父子关联 | session.parentID | agent labels（平铺 registry） | 抄 opencode 的 parentID（我们 session 模型天然适配） |
| 展示 | task 卡片跳子 session | mirror 事件 + 嵌套 transcript | 抄 opencode（子 session 独立视图，我们前端更简单） |

## 五、对我们落地方案的修订（在 opencode 讨论基础上的增量）

原方案见 `doc/subagent_discussion.md` 的五阶段。基于 kimi-code 调研，做以下修订/补充：

### 必须加的（来自 kimi-code）

1. **summaryPolicy 接入 task 工具**（阶段 2 内）：子 agent 跑完取最后一条 assistant 文本后，检查长度。太短（如 < 150 字符）就追加一条 user 消息"你的总结太简短，请补充：改了什么文件、为什么、如何验证"再跑一轮，最多重试 1 次。实现就十几行，但能显著提升 subagent 可用性--子 agent 经常一句话打发，父 agent 等于白等。
   - 注意：这个"续写"也要计入子 agent 的 history 和 token，要有 `max_steps` 兜底防失控。

2. **TASK_AGENT_ROLE_PREFIX 公共前缀**（阶段 1 内）：在 `system_prompt.txt` 之外，给所有 subagent profile 的 system prompt 拼一段固定前缀："你是 subagent，所有 user 消息来自主 agent，主 agent 看不到你的上下文、只能看到你最后一条消息，把主 agent 当调用方，不要直接问终端用户问题，不清楚的在总结里说明"。比每个 profile 各写一遍更不容易漏。

3. **wall-clock 超时**（阶段 2 内）：子 agent run 加超时（建议默认 10~30 分钟，比 kimi 的 2h 短，因为我们用 DeepSeek 成本/速度不同）。超时返回 `<task_error>` 并附 resume 提示。和 `max_steps` 并列，两者先到者触发。

### 修订的认知

4. **内置 profile 划分用 kimi-code 的**：`coder`（全能子 agent，能改码）+ `explore`（只读探索）。比 opencode 的 general+explore 更清晰--opencode 的 general 和 build 主 agent 几乎重合，kimi 的 coder 明确"唯一能改码的子 agent"定位更准。我们阶段 1 先做 explore（只读，最高频），coder 留到需要"把一个子任务整个外包（含改码）"时再加。

5. **并行优先抄 opencode 的简单方式**：`agent.py` 的 tool 执行段，遇到同一轮多个 task 工具调用就 `asyncio.gather` 并行跑。kimi 的 AgentSwarm（template+items+调度器+限流重试）是好东西但太重，等我们确认"主 agent 真的会频繁批量派生"后再考虑做成专用工具。

### 维持原方案的

6. **深度限制用 opencode 的 depth counter**：不学 kimi-code 的"靠白名单隐式"（coder 能套娃，有失控风险）。白名单（子 agent 不含 task 工具）+ depth counter（默认 1）双保险更稳。
7. **权限用白名单**：和 kimi-code 一致，不用 opencode 的三态 ruleset。
8. **后台任务暂不做**：kimi-code 的后台系统（TaskList/Output/Stop + notification XML + 跨重启 ghost）是完整但很重的子系统。我们阶段 1~3 只做前台同步 subagent。后台模式留到有明确需求（如"长跑测试+继续干别的"）再做，且可只做"注入 synthetic 消息"的轻量版，不抄整套任务管理工具。
9. **父子关联 + 展示抄 opencode**：session.parentID + 子 session 独立视图 + task 卡片跳转。比 kimi 的 mirror 事件流更适合我们的前端结构。

## 六、一句话总结

opencode 给了我们 subagent 的**骨架**（声明式 agent + task 工具 + 父子 session + 深度限制），kimi-code 给了我们三个**值得单拎出来抄的细节**（summaryPolicy 结果兜底、通用角色前缀、wall-clock 超时）和两个**暂时不抄的重活**（AgentSwarm 调度器、后台任务管理系统）。两者都证明了 subagent 的核心价值一致：**隔离上下文、省主 agent token、并行**。我们按 opencode 讨论的五阶段走，把 kimi-code 的三个细节补进阶段 1~2，就能拿到 subagent 的主要价值，且不过度工程。

---

## 附：kimi-code 关键文件索引

- `packages/agent-core-v2/src/session/subagent/tools/agent.ts` - Agent 工具主体（launch/resume/前后台/超时）
- `packages/agent-core-v2/src/session/subagent/tools/agent.md` - Agent 工具描述（提示词工程，写得很好，值得读）
- `packages/agent-core-v2/src/session/subagent/runAgentTurn.ts` - 跑一轮 + **distillSummary 蒸馏质量控制**
- `packages/agent-core-v2/src/session/agentLifecycle/profile/summary-continuation.md` - 续写 prompt
- `packages/agent-core-v2/src/session/agentLifecycle/profile/profiles.ts` - 内置 profile（agent/coder/explore）+ summaryPolicy
- `packages/agent-core-v2/src/agent/plan/profile/plan.ts` - plan profile
- `packages/agent-core-v2/src/app/agentProfileCatalog/profile-shared.ts` - TASK_AGENT_ROLE_PREFIX + allowlist + renderSystemPrompt
- `packages/agent-core-v2/src/session/agentLifecycle/profile/explore-overlay.md` - explore 角色 prompt + promptPrefix(git context)
- `packages/agent-core-v2/src/session/subagent/mirrorAgentRun.ts` - 调用方镜像 + subagent.* 事件
- `packages/agent-core-v2/src/session/swarm/sessionSwarmService.ts` + `agentRunBatch.ts` - AgentSwarm 调度器（并发/限流挂起/重试）
- `packages/agent-core-v2/src/agent/swarm/tools/agent-swarm.ts` + `.md` - AgentSwarm 工具
- `packages/agent-core-v2/src/agent/swarm/enter-reminder.md` / `exit-reminder.md` - swarm 模式提示
- `packages/agent-core-v2/src/agent/task/taskService.ts` + `notificationXml.ts` - 后台任务管理 + `<notification>` 注入
- `packages/agent-core-v2/src/session/subagent/configSection.ts` - 超时配置（2h 默认）
- `packages/agent-core-v2/src/session/agentLifecycle/subagentMetadata.ts` - 父子关系 labels
