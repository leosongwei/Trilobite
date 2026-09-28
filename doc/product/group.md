# Group（群聊模式）

## 概述

Group Session 是一个多 agent 聊天频道：用户在频道里发布指令，由多个 Agent 共同完成用户交代的任务。频道本身不运行 LLM——它是消息的记录者与分发者；每个 Agent（成员）是一个完整的 agent（全套编码工具），拥有一个名字，通过 `send_message` 工具与用户和其他成员交流，团队自行组织分工。

成员以 child session 的形式挂在 group session 下面（与 subagent 同构的树状展示），可以点进去看它的完整工作过程、直接给它发消息、中途引导或停止。

## 生命周期

1. **创建**：sidebar 顶栏 `[+ Group]`（或项目行的 group 图标按钮）创建一个空的 group channel。工作目录可填可不填：填了则以该目录为工作区，不填自动在 session 文件夹内建 `chat_files/` 并以其为工作目录（与 chat 模式同规则）。`session.json` 记录 `mode: "group"` 与空的 `group_members` 花名册。
2. **确认成员数**：切到该 session 后，聊天窗口中央弹出确认框，选择创建多少个 Agent（1-16）。确认后成员被创建，花名册固化——每个成员获得一个互不重复的名字，`group_members`（名字 → session id 映射）持久化；重复调用 spawn 会被拒绝。
3. **发布指令**：用户在频道输入框发言，消息进入频道历史（发送者为 user），并投递给**每一个**成员：运行中的成员以 steering 消息拾取，空闲的成员以它作为第一条 user 消息启动新 run。成员各自阅读、互相协调、用工具干活，向频道汇报。
4. **长期共存**：成员是有生命的队友而非一次性 subagent——run 结束只是回到空闲（等待下一条频道或私信），不密封、可反复驱动；历史长期可查。

## send_message 工具

成员在系统提示词中知晓自己的名字和所有同伴的名字；频道协议也在系统提示词中写明：

- 成员的**纯文本回复只有它自己的会话视图能看到**——频道里唯一的发声方式是 `send_message(to, text)`。
- `to` 的取值只有两种：`"all"`（频道里的所有人——用户和除自己外的全部成员）、或某个同伴的名字（定向投递那一个成员）。未知名字返回错误并列出合法取值，模型可一次纠正。
- 每条消息都会落频道历史（发送者为该成员的名字，前端渲染头像 + 名字），用户在群聊视图始终全量可见；`to` 只决定**哪些成员的上下文会被插入这条消息**——投递路径同样是"运行中 steering / 空闲启动"两种。
- 成员收到的消息在它的历史里形如 `[from X]\n内容`（X 为 user 或成员名），模型据此区分说话者。
- 协调先行：有多人空间的任务，成员的第一步是在频道里提出分工方案并**认领自己的部分**（`send_message("all")`），协调完重叠认领后才动手干活；无人反对即视为认领成立，不空等审批。琐碎的单步任务可以只报备一句就做。

## 频道视图（主 session）

- 聊天窗口呈**群聊**形态：只渲染频道历史里的 user 消息；成员消息显示头像（按名字散列着色）+ 名字 + 中性气泡，用户消息保持常规样式。成员的思考/工具过程不在频道里，点进成员会话查看。
- 群聊顶栏显示成员 chips（头像点 + 名字，点击切入该成员会话），运行中的成员带绿点；chip 旁的提示说明"发到频道即投递给所有成员"。
- 发送框始终可用（成员运行中发消息即 steering），图片与 `/compact` 命令在频道中不提供。
- Token 栏对频道隐藏（频道无 token 概念），成员会话各自显示。
- 成员数确认框只在花名册为空时出现；确认后消失，不可重开。

## 成员会话视图

- 与 subagent 视图同构：subagent-bar 显示 `group` 标签与成员名字，back 按钮返回频道。
- 输入框可用（直接对该成员发消息，作为普通 user 消息插入它的历史，不带 `[from]` 标签）；运行中即为 steering。
- **停止 = cancel**（与有界 subagent 的 interrupt+总结+密封不同）：硬停当前工作，成员留在花名册中可继续使用。
- sidebar 中成员以群组图标徽章显示在 group session 下，按名字排序；删除成员只移除该成员（频道与其余成员不受影响）。

## 权限与隔离

- 成员的权限是 `GroupMemberPermission`：读/搜/编辑/写/bash/skill + `send_message`。没有 `task`（无嵌套派生）、没有 `sleep_until`、没有 `TodoList`。
- 权限请求沿用 subagent 的全局广播：成员请求授权时，频道与全部成员的流都会收到 `subagent_permission_request`（标注成员身份），横幅与 Pending Requests 列表审批，批准写入该成员自己的 `additional_dirs`。
- 频道自身的 `cancel` 向全部运行中成员传播（硬停）；成员之间互不传播。
- 模型以**群主 session 当前选定**为准，成员不单独选择模型：成员的每一次 LLM 调用在**回合开始前**同步频道当前模型（并持久化到成员 `session.json`）。用户在群会话上随时切换模型，成员的下一次调用即生效；**正在执行中的请求不受影响**，以旧参数跑完本次调用。无需在创建成员之前选好模型。

## 数据与持久化

```
sessions/
  <group_id>/                 # group channel（mode: "group"）
    session.json              # name, working_dir, mode, group_members: {名字: session_id}, group_size
    history.json              # 频道历史：仅 user 消息（sender 字段标明 user / 成员名）
  <member_id>/                # 成员（与频道平级，扁平结构）
    session.json              # parent_session, subagent_type: "group", member_name, description,
                              # group_peers（同伴名字列表）, depth, working_dir, model, ...
    history.json              # 成员自己的完整工作历史（system + 频道投递 + 工具调用）
```

- 频道的历史条目是带 `sender` 字段的 UserMessage（`sender` 为空即普通会话，投影 API 时忽略）；成员历史中的投递消息把说话者以 `[from X]` 前缀写进正文。
- 服务重启后按磁盘重建：频道读回花名册，成员重建为**未密封**的可交互 agent；成员实例在第一次被频道投递或被直接访问时惰性物化，重启期间未投递的消息照常投递。
- 删除频道级联删除全部成员（沿用父子级联）。

## 入口

- sidebar 顶栏创建按钮为 `[+ Session][+ Chat][+ Group][+ Project]`；`+ Group` 使用顶栏工作目录输入框（可空）。
- 项目行操作按钮为 铅笔、`+`、group 图标、`×`：group 图标以项目的工作目录创建归属该项目的 group session（与 `+` 创建普通 session 并列）。
