# WBS（项目任务管理）

## 概述

WBS（Work Breakdown Structure）是挂在 **Project** 下的任务分解表，一个 project 一张，用于把一项工作拆成可执行的单元、驱动多个 agent 协作完成并支持复审迭代。四根支柱：

* **结构**：**workitem（工作项）→ task（任务）** 两层，task 是叶子；workitem 之间不嵌套，task 只属于一个 workitem。
* **顺序**：**pre-condition** 描述条目之间的依赖与时间门槛；**`task_num`** 描述同一 workitem 内任务的先后阶段。
* **角色与委派**：每个 task 带一个 **`assignment`（角色）**。**同一 workitem 内、同一角色的 task 由同一个会话完成**——派发单位是 **task**，会话键是 **(workitem, 角色)**。所以一个 workitem 的会话数取决于它用到的角色数（例：`dev` 一个会话、`autoreview` 一个会话）。
* **复审与迭代**：`review` 型任务的会话完成时**必须选 `pass` 或 `reject`**；`reject`（打回）把本轮涉及的 task 整体复制为新的一轮（旧任务保持关闭），复用 `task_num`、继续推进。

WBS 与 session 的连线只有两条：**调度器**派生干活会话；agent 经虚拟工具 **`project_management_tool`** 读写 WBS。管理界面是用户主动操作，不走权限审批（参见 [file_manager.md](./file_manager.md)）。

入口：在 sidebar **进入**某个 project 后，其成员 session 列表之前有一行 “Task management”（图标 + 文案）；点击后主视图切换为 WBS 面板（与文件管理器同构的**主视图替换**，参见 [projects.md](./projects.md)）。

## 数据模型

每个条目（workitem 与 task）都有恒等标识 `_id`（uuid），**workitem 与 task 共用一个 `_id` 表示空间**——它是条目互相引用（pre-condition 指向、task 归属 workitem）的唯一依据。面向用户的是可读编号。

### 可读编号

* `workitem_id`：从 1 开始、单调递增的整数，用于 WorkItem；显示为 `WI` + 补零。
* `task_id`：从 1 开始、单调递增的整数（与 `workitem_id` 各用各的计数器），用于 Task；显示为 `TSK` + 补零。
* 补零宽度 `width` 是 **project 级** 的值，workitem 与 task 共用：初始为 **6**，每当某个编号的十进制位数达到当前 `width` 时，`width` 整体 **+3**（6 → 9 → 12 → …）。判定以「已分配过的最大编号」（计数器 − 1）为准，只增不减——删除条目不会让已展示过的编号变短，也不回收编号。
  * 编号 123 → `WI000123`；编号到 100000（6 位）时宽度升到 9，此后 123456 → `WI000123456`。

### 角色（assignment）

`assignment` 标明由谁来做这个 task，也是会话的绑定维度。**角色是用户自定义的任意字符串**（`dev`、`design`、`reviewer`、`autoreview`……字符上不同即视为不同角色；无需预先注册，也不新增存储抽象——它就是 task 上的一个字符串）。**只有 `user` 是特殊值**：

* `user`：不由 agent 会话完成，留给用户（如最终 review），进入界面的 **My tasks**。
* 其他任意字符串：每个不同取值在需要时获得一个干活会话。

规则：

* 同一 workitem 内、同一角色（字符串相同）的 task 共用一个会话。**一个 workitem 的会话数 = 它用到的不同非 `user` 角色数**（对所有 task 的 `assignment` 去重即得）。
* `review` 型任务可由任意角色承担；其会话完成时按类型要求 `pass` / `reject`（见「迭代」节）。

### WorkItem

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `_id` | uuid | 内部恒等标识，与 task 共用表示空间 |
| `workitem_id` | int | 可读编号的整数部分，从 1 递增 |
| `name` | str | 简短名称 |
| `description` | str | 详细描述 |
| `status` | str | `todo` / `doing` / `done`，默认 `todo` |
| `pre_conditions` | PreCondition[] | 前置条件，可空 |
| `created_at` / `updated_at` | float | epoch 秒 |

### Task

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `_id` | uuid | 内部恒等标识，与 workitem 共用表示空间 |
| `workitem` | uuid | 所属 workitem 的 `_id` |
| `task_id` | int | 可读编号的整数部分，从 1 递增 |
| `task_num` | int | 阶段序号，见下 |
| `type` | str | 任务类型：`normal`（普通）或 `review`（复审） |
| `assignment` | str | 角色，自定义任意字符串；`user` 特殊 |
| `name` | str | 简短名称 |
| `description` | str | 详细描述；**仅人工创建的会话（及用户界面）可修改，干活会话不可改**，视为任务规格 |
| `notes` | str | 备注文本，由干活的会话维护（review 结论也写在这里） |
| `status` | str | `todo` / `doing` / `done`，默认 `todo` |
| `pre_conditions` | PreCondition[] | 前置条件，可空 |
| `session_id` | str \| null | 实际完成该 task 的会话（= 所属 `(workitem, 角色)` 的会话；`user` task 为空） |
| `created_at` / `updated_at` | float | epoch 秒 |

`task_id`（`TSK000123`，全局唯一句柄）与 `task_num`（阶段号）是两个不同的概念，都展示。task 的三个内容要素：**`name`（标题）、`description`（规格）、`notes`（干活会话写的备注）**。迭代轮次**不单独存储**，由 `name` 的 `-iteration x` 后缀表达（见「迭代」节）。

**`task_num` 是同一 workitem 内的阶段号，也是就绪顺序**：`task_num` 相同的 task 属于同一阶段、可并行；`task_num` 更大的 task，必须等**所有 `task_num` 更小的 task 全部 `done`** 之后才能开始。**迭代任务复用被迭代任务的 `task_num`**（见下）。

### 任务类型（type）

task 分两种类型：

* `normal`（普通任务）：干活会话完成后直接置 `done`。
* `review`（复审任务）：用于对上一段工作把关。会话结束复审时**必须二选一**——`pass`（通过）或 `reject`（打回），并填写 review notes（写入该 review 任务的 `notes`，结论也在其中）。`pass` 仅收尾；`reject` 触发一轮迭代（见「迭代」节）并生成下一轮任务。**结论不单独设字段**，就落在 notes 里。

### PreCondition

前置条件挂在 workitem 或 task 上，每条是一个必达的门槛，一个条目可挂多条、**全部满足**才算就绪。带标签的联合：

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `_id` | uuid | 本身恒等标识 |
| `kind` | str | `dependency`（依赖某条目）或 `time`（等到某时刻） |
| `target_kind` | str | 仅 `dependency`：`workitem` 或 `task` |
| `target_id` | uuid | 仅 `dependency`：目标条目的 `_id`（同 project 内） |
| `at` | str | 仅 `time`：本地时间 `YYYY-MM-DD HH:MM`（与 timer 同格式） |

满足性：`dependency` 满足 ⟺ 目标条目 `status == "done"`；`time` 满足 ⟺ 当前本地时间 ≥ `at`。

### 就绪、可派发

* **条目就绪**：该条目所有 pre-condition 满足。
* **task 就绪**：task 自身就绪 **且** 所属 workitem 就绪 **且** 同 workitem 内所有 `task_num` 更小的 task 都已 `done`。`task_num` **相同**的 task 互不阻塞，可并行（哪怕角色不同，各自开自己的会话同时启动）。
* **task 可派发**：task 就绪 **且** `assignment` 是非 `user` 角色 **且** `status == "todo"`。
* 就绪 / 阻塞是状态判定：`user` task 的就绪用于提示用户，其他角色的就绪用于触发调度器。workitem 的 `status` 与其 task 状态不自动联动（各自维护），workitem 行按「done task 数 / task 总数」显示进度。

## 迭代（打回）

`review` 型任务的会话结束复审时**必须选 `pass` 或 `reject`**：

* **pass**：把该 review task 置 `done`，review notes（含 `pass` 结论）写入其 `notes`，本轮通过。
* **reject（打回）**：把该 review task 置 `done`，review notes（含 `reject` 结论）写入其 `notes`，并把本轮涉及的 task **整体复制**为新的一轮接力——旧任务保持 `done` 关闭，因此复用的 `task_num` 不会冲突。

**一轮的范围**：打回由某个 review task `R`（`type = review`）触发；取 `R` 之前最近的 review task `P`（`task_num` 最大且小于 `R` 的 `type = review` 任务，可能不存在）。复制 `task_num` 落在 `(P, R]` 区间内的**全部** task（`P` 不含，含 `R` 自己）。首次 review 没有 `P`，则复制从头到 `R`（含）。

以 `function xxx` 为例，初始：

| task | task_num | type | role | status |
| --- | --- | --- | --- | --- |
| design | 1 | normal | dev | todo |
| design review | 2 | review | autoreview | todo |
| coding | 3 | normal | dev | todo |
| code review | 4 | review | autoreview | todo |
| review | 5 | review | user | todo |

`autoreview` 会话在 `design review` 上 `reject`，复制区间 `(无, 2]` = `design` + `design review`，workitem 变为：

| task | task_num | type | role | status |
| --- | --- | --- | --- | --- |
| design | 1 | normal | dev | done |
| design review | 2 | review | autoreview | done（reject） |
| design -iteration 1 | 1 | normal | dev | todo |
| design review -iteration 1 | 2 | review | autoreview | todo |
| coding | 3 | normal | dev | todo |
| code review | 4 | review | autoreview | todo |
| review | 5 | review | user | todo |

规则：

* **迭代轮次不新增存储**：由 `name` 的 ` -iteration x` 后缀表达——打回时解析源任务名后缀里的 `x`（没有视为 0），新任务名 = 去掉旧后缀 + ` -iteration <x+1>`；再次打回继续递增。既用于命名，也用于确定轮次。
* 复制出的任务：`task_num`、`type`、`role`、`description` 不变；`status = todo`；`session_id` 清空；名称按上式加后缀；review notes（拒收意见）随复制带入新任务的 `notes`，干活会话在自己的 notes 里直接读到；指向本轮内任务的 `dependency` 前置条件重定向到对应副本，其余前置条件原样复制。
* 因复用 `task_num` 且旧任务已 `done`，不会冲突；就绪规则把新一轮串起来：`design -iteration 1`(num 1) 就绪 → `design review -iteration 1`(num 2，须等所有 num 1 完成) → … → `coding`(num 3，须等所有 num 1、2 完成)……再次打回则再叠一轮，链式继续。
* 复制出的任务就绪后，调度器**追加派发**给对应角色的已有会话（`dev` 会话、`autoreview` 会话），无需新建会话。
* 更靠后的 review 同理：`code review`(num 4) reject 时 `P` = 其前最近的 review（num 2 那一轮），复制 `(2, 4]` = `coding` + `code review`。

## Workitem 模板

用户可以定义可复用的 **workitem 模板**：按 project 持久化的独立实体，用一份带占位符的 workitem 蓝图 + 一组输入参数，一键生成结构相同的 workitem。

模板包含：

* `name`：模板名。
* `input_keys`：字符串列表——实例化时需要提供的输入参数名。
* `body`：workitem 蓝图，含 workitem 的 `name`/`description` 与有序的 task 蓝图列表（每项 `task_num`、`type`、`role`、`name`、`description`）。蓝图中任意字符串可用 `<key>` 占位符引用输入参数。

**实例化**：调用方提供一个输入字典（键 = `input_keys` 的元素）；系统把 `body` 里所有 `<key>` 替换为对应值，生成一个普通 workitem（task `status = todo`、无 `session_id`）。缺键报错并列出 `input_keys`。生成后即普通 workitem，可自由增删改。

示例——编码任务模板：

* `input_keys = ["name", "description", "coding_prompt", "review_prompt"]`
* `body`：
  ```
  name:        "coding work: <name>"
  description: "desc: <description>"
  tasks:
    - { task_num: 1, type: normal, role: dev,       name: design,        description: "" }
    - { task_num: 2, type: review, role: autoreview, name: design review, description: "<review_prompt>" }
    - { task_num: 3, type: normal, role: dev,       name: coding,        description: "<coding_prompt>" }
  ```
* 创建时填入 `{name: …, description: …, coding_prompt: …, review_prompt: …}` 即生成对应 workitem。

**填写方式**：人用面板里的**图形化编辑器**——按 `input_keys` 生成表单字段，填完即生成 workitem；模型经 `project_management_tool` 的 `use-template` 命令传入键值。

模板可增删改查；也可把一个已有 workitem 另存为模板。模板只定义结构与角色字符串，不含前置条件与运行时状态；实例化后可再补 pre-condition。

## 持久化（SQLite）

WBS 数据存于**单个 SQLite 库** `get_sessions_dir()/wbs.sqlite3`（`get_sessions_dir()` 见 [projects.md](./projects.md)）；project 列表仍在 `projects.json`，库内以 `project_id` 关联。

### 为什么用 SQLite（而不是 JSON）

WBS 有三类**并发写者**：用户在管理界面的 HTTP 请求、agent 会话里的 `project_management_tool`、调度器的自动派发。JSON 是整文件读改写，并发下会**丢失更新**（后写覆盖先写）。SQLite 提供：

* 事务 + 库级锁，写操作串行化，不丢更新；
* 提交原子性，崩溃不会留下半截文件；WAL 下已提交事务落盘；
* 用外键表达「删 workitem 级联删 task、删条目级联清除指向它的 pre-condition」，无需应用层维护悬空引用；
* 依赖解析、可派发 task 扫描都是天然查询。

### 库表

```sql
-- 条目：workitem 与 task 共表，天然表达共用 _id 空间
items(
  _id         TEXT PRIMARY KEY,      -- uuid
  project_id  TEXT NOT NULL,
  kind        TEXT NOT NULL,         -- 'workitem' | 'task'
  num         INTEGER NOT NULL,      -- workitem_id 或 task_id
  parent_id   TEXT REFERENCES items(_id) ON DELETE CASCADE,  -- task -> workitem
  task_num    INTEGER,               -- task 专用：阶段号（迭代复用）
  type        TEXT NOT NULL DEFAULT 'normal',  -- task 专用：'normal' | 'review'
  assignment  TEXT,                  -- task 专用：角色（任意字符串）
  name        TEXT NOT NULL,
  description TEXT NOT NULL DEFAULT '',
  notes       TEXT NOT NULL DEFAULT '',   -- 备注文本（review 结论也写在这里）
  status      TEXT NOT NULL DEFAULT 'todo',
  session_id  TEXT,                  -- task 专用：完成它的会话
  created_at  REAL NOT NULL,
  updated_at  REAL NOT NULL
)

pre_conditions(
  _id        TEXT PRIMARY KEY,
  item_id    TEXT NOT NULL REFERENCES items(_id) ON DELETE CASCADE,  -- 所有者
  kind       TEXT NOT NULL,          -- 'dependency' | 'time'
  target_id  TEXT REFERENCES items(_id) ON DELETE CASCADE,           -- dependency 目标
  at         TEXT                    -- time 到点
)

-- 会话绑定：一个 (workitem, 角色) 至多一个干活会话，在 workitem 生命周期内复用
role_sessions(
  workitem_id TEXT NOT NULL REFERENCES items(_id) ON DELETE CASCADE,
  role        TEXT NOT NULL,         -- 'dev' / 'autoreview' / ...
  session_id  TEXT NOT NULL,
  attempts    INTEGER NOT NULL DEFAULT 0,
  created_at  REAL NOT NULL,
  PRIMARY KEY(workitem_id, role)
)

-- 单调计数器，删除不回退（避免复用编号）
counters(project_id TEXT NOT NULL, kind TEXT NOT NULL, next INTEGER NOT NULL,
         PRIMARY KEY(project_id, kind))

-- workitem 模板：input_keys 为输入参数名数组（JSON），body 为带 <key> 占位符的 workitem 蓝图（JSON）
templates(
  _id        TEXT PRIMARY KEY,
  project_id TEXT NOT NULL,
  name       TEXT NOT NULL,
  input_keys TEXT NOT NULL,
  body       TEXT NOT NULL,
  created_at REAL NOT NULL,
  updated_at REAL NOT NULL
)

-- schema 版本，便于迁移
meta(key TEXT PRIMARY KEY, value TEXT)
```

`(project_id, kind, num)` 建唯一索引；items 按 `project_id`、`(project_id, kind, num)`、`(parent_id, task_num)` 建索引。

### 避免数据损失

* **WAL + 事务**：`PRAGMA journal_mode=WAL`、`PRAGMA synchronous=NORMAL`；所有多行改动放在一个事务里（写用 `BEGIN IMMEDIATE`）。掉电最多丢最后若干次提交，不会损坏库。
* **外键开启**：每条连接 `PRAGMA foreign_keys=ON`，级联删除交给数据库。
* **单写者**：一个进程内用一把锁把写操作串行化（`sqlite3` 同步 API，写操作短小）。
* **启动自检**：`PRAGMA integrity_check`，异常则告警并保留原文件而不是静默重建。
* **定时快照**：每日 `VACUUM INTO 'wbs-YYYYMMDD.sqlite3'`（或文件副本），保留最近 N 份，作为用户数据的兜底。
* **schema 版本**：`meta.version` 驱动一次性迁移。
* 删除 project 时删除该 `project_id` 的全部行与计数器。

## 调度器（拉起干活会话）

一个常驻的 `WbsScheduler`，随 server 启动装配（与 `TimerService` 并列）。

* **派发单位是 task，会话键是 `(workitem, 角色)`**：扫描可派发的 task，按 `(workitem, assignment)` 归组；每组对应一个干活会话，同组 task 由它完成。`user` 角色不派发。
* **触发**：① 每次 WBS 变更（新建 / 打回迭代 / 改状态 / 改 `task_num` / 改 `assignment` / 改 pre-condition）后立即评估；② 周期 tick（约 5s）兜底 `time` 类前置到点与 `user` task 完成后解锁。
* **扫描**：对每个 project 取「可派发」的 task，按 `(workitem, 角色)` 归组。
  * **该组无绑定会话** → 创建干活会话并派发；
  * **该组已有绑定会话** → 把新解锁的 task（含打回生成的重做任务）**追加派发**给它（运行中则以 steering 投递，空闲则以新 run 启动）。
* **创建干活会话**：普通 session——`session.json` 记 `working_dir = project.working_dir`、`project_id`、`wbs_workitem = <workitem _id>`、`wbs_role = <角色>`，自动标题 `WI000123/<角色> <name>`。会话**直接隶属于 project**（不挂在 task 下），sidebar 用**电脑图标**与人工创建的 session 区分。
* **派发内容**：一条任务简报（workitem 编号/名称/描述、本次可做的 task 及其编号/名称/类型/描述、复审反馈摘要，以及「用 `project_management_tool` 查看上下文；普通任务完成后 `done <TSK>`；review 任务复审后 `done <TSK> pass|reject <notes>`」的指示），创建会话时作为首条 user 消息，复用会话时作为后续消息。
* **标记**：派发时把相关 task 置 `doing`。task 完成由会话经工具置 `done`（并记 `session_id`、可留 notes）。`role_sessions.attempts` 每次派发 `+1`。
* **并发上限**：全局 `wbs_max_concurrent_sessions`（默认 2）限制同时**运行**的干活会话数，达到上限则本轮跳过，避免无人值守燃烧 token。
* **幂等**：`status != todo` 的 task 不重复派发；同一 `(workitem, 角色)` 只建一个会话。
* **级联推进**：某 task 被置 `done` 后，下一轮评估自动派发其后继（同 workitem 的下一阶段 task、同一打回触发的重做任务、或依赖它的其他条目）；`user` task 由用户在界面置 `done` 同理。
* **重启恢复**：`role_sessions` 里指向的 session 已不存在的行清除（下次重建）；`status=doing` 但所属会话已不存在且未 done 的 task 回退 `todo` 重新参与。`attempts` 超过阈值（如 3）则不再自动重派，界面标红提示，留给用户处理。
* **边界（project / task 删除）**：删除 project 时其 WBS 行全部删除；该项目下正在运行的干活会话按现有「删除 project 只解除归属」语义保留、变为未分组，其后续工具调用会因「不属于任何 project」而报错。删除某个 task 时其界面入口消失，干活会话不删除。

## Agent 工具（`project_management_tool`）

一个**虚拟工具**（定义在 `tool_call.py`，执行逻辑在 `Agent`，与 `task` / `sleep_until` / `send_message` 同构），让 agent 以**命令风格**读写当前 project 的 WBS。参数为 `list[str]`（位置化命令与实参）；**工具描述只提几个基本功能**，完整用法由 `help` 返回，以节约上下文。命令用可读编号寻址（`WI…` / `TSK…`），与界面一致。

工具从当前会话的 `project_id` 解析作用域；会话不属于任何 project 时报错。**能力按会话来源分档**（来源记录在 `session.json`：带 `wbs_workitem` 的是干活会话，否则为人工创建）：

### 人工创建的会话——可读可写

* 命令：`help`、`list [workitem|task|mine]`、`show <编号>`、`status <编号> <todo|doing|done>`、`assign <TSK> <角色>`、`add-workitem <name> [description]`、`add-task <WI> <task_num> <type> <name> [description] [assignment]`、`edit <编号> [name=…] [description=…]`、`note <TSK> <text>`、`templates`、`show-template <模板>`、`use-template <模板> <key>=<value> [...]`。
* 即：可以查询、改状态、改派、**创建 workitem / task（含从模板生成）**、**改 name / description**（人工会话专属）、加备注（`<type>` = `normal` / `review`）。

### 干活会话——只标记完成、写 notes

* 普通任务：`done <TSK> [text]`。
* `review` 型任务：`done <TSK> pass|reject <notes>`——**必须给出结论与 review notes**（`reject` 触发一轮迭代，见「迭代」节）。
* 另有 `help`、`list`、`show <编号>`、`note <TSK> <text>`。
* 即：**只能把自己绑定范围内的 task（同 workitem、同角色）标记 `done`（review 需附 pass/reject）、只能为这些 task 追加 notes**。不能自由创建条目、不能改 `status` 为其他值、不能改 `assignment`，**不能修改 `name` / `description`**（description 只由人工创建的会话与用户界面改）。
* 读取范围是整张 WBS（便于理解依赖），写入范围仅限自己绑定的 `(workitem, 角色)` 下的 task。

### 可用范围

主 agent、general subagent、group 成员可用；**explore subagent 不可用**。人工创建会话的子 agent 继承人工档（可创建）；干活会话一般不再派生 subagent（如需则继承干活档）。工具描述里只写 `help` / `list` / `done`（`done` 兼顾 pass/reject）这类基本功能，细节靠 `help`。

### 输出

紧凑、面向 token；错误信息指明合法取值，便于模型一次纠正。

## 后端 API

除 `project_management_tool` 外，用户界面用 REST，全部在 `/api/projects/{project_id}/wbs` 下，沿用现有约定（模块级 pydantic 请求模型、`async def`、`HTTPException`、`_id` 作路径参数）。project / 条目不存在返回 404。

* `GET /api/projects/{project_id}/wbs` — 返回 `{width, workitems, tasks}`（含就绪/阻塞派生结果、`type`、`session_id`、`notes`、各 `(workitem, 角色)` 的会话绑定）。
* `POST .../workitems`、`PUT .../workitems/{id}`、`DELETE .../workitems/{id}`（`POST` 可带 `template_id` + `input` 从模板实例化）。
* `POST .../workitems/{id}/tasks`、`PUT .../tasks/{id}`、`DELETE .../tasks/{id}`。
* `POST .../tasks/{id}/dispatch` — 用户手动拉起该 task 所属 `(workitem, 角色)` 的干活会话（无则创建）。
* `POST .../tasks/{id}/reset` — 把停滞 task 复位 `todo`（清 `session_id`，可重置对应绑定 `attempts`）。
* `GET/POST .../templates`、`PUT/DELETE .../templates/{id}` — workitem 模板增删改查（含 `input_keys` / `body`）。

`pre_conditions` 由客户端整表提交，服务端为缺 `_id` 的条目补发 `_id` 并整体替换。`description` 在 PUT 中可改（用户），但干活会话无此权限。列表按 `(task_num, created_at)` 升序返回。

## 前端

### sidebar 入口

* 进入某个 project 时，在成员 session 列表**之前**渲染一行 `Task management`（`session-item` 样式 + 清单图标），点击打开该 project 的 WBS 面板；面板打开时该行高亮。
* 行尾可带徽章：**就绪的 user task 数**（提醒用户有待处理事项）。

### sidebar 会话区分

* 干活会话（`session.json` 带 `wbs_workitem`）在 session 行显示**电脑图标**（如 `ms-computer`），与人工创建的 session（人形/默认图标）区分；同样直接排在该 project 下。

### 主视图面板

主视图整块替换为 WBS 面板（与 `FileManager` 的 `v-if/v-else` 切换同构）。

* **顶栏**：`Task management` 标题 + project 名称 + 返回按钮（回到对话视图）。
* **视图/筛选**：全部 / 未完成 / 已完成 / 阻塞 / **My tasks**（`assignment=user`，就绪的排前）——My tasks 即用户要做的（如最终 review）集中入口。
* **列表**：workitem 逐行——可读编号 `WI000123`（等宽，点击复制）、名称、状态徽章、就绪徽章（“就绪” / “阻塞：N 项未满足”）、task 进度（`done/total`）、该 workitem 各角色的干活会话 chips（电脑图标 + 角色名，点击切入）、行尾操作（编辑 / 删除 / `+ Task`）；其下缩进列 tasks——`TSK000456`、`task_num`、名称、`type`（normal/review）、`assignment` 角色标记、状态徽章、review 任务的 notes 摘要（含 `pass`/`reject` 结论）、就绪徽章、行尾操作。tasks 按 `(task_num, created_at)` 升序，同一轮迭代（`-iteration x`）与原任务相邻。
* **模板**：`+ Workitem` 支持从 workitem 模板一键生成——选中模板后弹出**图形化输入表单**（按 `input_keys` 生成字段），填完即建好 workitem（含各 task）；另有模板编辑（名称 / `input_keys` / 蓝图 `body`）与把现有 workitem 另存为模板。
* **task 详情**：展示 `name` / `description` / `notes`（含 review 结论与打回反馈）；agent task 显示完成它的会话入口。
* **编辑**：新建/编辑用居中模态（沿用 `GroupSetupDialog` / 模型弹窗样式）：名称、描述、类型（normal/review）、状态、`task_num`、`assignment`，以及 pre-condition 编辑器——每行选 `依赖`（目标选择器：列出全部 WI/TSK 编号与名称，排除自身）或 `时间`（本地时间输入），可增删多行。名称输入框旁常驻长度提示（“≤20 词 / ≤40 字”），仅提示、不限制。**编辑模态供用户使用，description 可改**；干活会话无此界面。
* **复审收尾**：`user` 角色的 `review` 任务在界面收尾时必须选 `pass` / `reject` 并填 review notes（与干活会话同语义），`reject` 同样生成下一轮。
* **删除**：`confirm()` 确认，提示将一并删除的 task 数与将被清除的依赖数。
* **手动操作**：可手动拉起某 task 所属角色的干活会话、停掉正在运行的干活会话、复位停滞 task。
* **实时刷新**：面板打开期间，agent（经工具）与调度器也会改动 WBS，因此面板周期重取（复用 sidebar 的 3s 轮询节拍或独立轻量轮询），保证状态、就绪徽章、notes、会话关联实时。
* **空态**：`还没有工作项，点击 + Workitem 开始分解。`

## 校验与约束

* `name` 必填非空；长度仅 UI 提示（≤20 词或 ≤40 汉字），**数据层不设限**。
* `task_num` 为正整数；`assignment` 为任意非空字符串；`type ∈ {normal, review}`（`review` 任务完成时必须给出 `pass`/`reject` 结论与 notes）。
* 模板 `input_keys` 为字符串数组；实例化时须覆盖全部 `input_keys`，缺键报错；`body` 中未识别的 `<key>` 原样保留。
* pre-condition 的 `dependency` 目标必须存在且属于同一 project；禁止自引用；保存时检测依赖环，有环则拒绝。
* `time` 的 `at` 为 `YYYY-MM-DD HH:MM` 本地时间；非法格式拒绝。
* 删除 workitem 级联删除其 task；删除条目由外键自动清除指向它的 pre-condition。

## 配置

新增到 `config.yaml`：

* `wbs_auto_dispatch`（bool，默认 `true`）——是否开启自动派发。
* `wbs_max_concurrent_sessions`（int，默认 `2`）——同时运行的干活会话全局上限。

（角色集合、tick 间隔等暂为内置常量；角色后续可配置。）

## 非目标

* 不跨 project 建立依赖。
* workitem 不嵌套（只有两层）。
* 无甘特图 / 时间线视图。
* 无拖拽排序。
* 无到点/就绪的通知推送（用户侧靠 My tasks 与徽章被动查看）。
