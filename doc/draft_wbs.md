# WBS.

状态：草稿，不要遵照此文档！

起草一个 doc/product/wbs.md 

用比较典型的项目管理上的 work breakdown structure ，拿来做agent调度。

# 数据定义

有一个递归抽象：WorkItem，形式如：

```python
class WorkItem: # 简称WI
    wi_number: int ~ 主键 # 数据库主键，从1开始，显示时按"WI"加五位数填充（如 WI00123）
    sequence_num: int # 序列号。当一个WI属于一个父级WI时，用来决定其执行顺序。
    create_time: date
    start_time: date # 记录WI最后一次被标记为WRK的时间
    complete_time: Optional[date] # 记录WI

    status: enum ~ ASN (assigned) | WRK(working) | CAN(Cancelled) | CLS(Closed) # 这几种状态
    role: str ~ ROLE # 角色，对于非原子WI无效

    is_atomic_wi: bool # 判定是否是原子WI（有tasks时，就不是原子WI了），建议实现为getter
    tasks: List[wi_number] # 子序列，递归定义，一个WI可以属于一个父级WI，但不能属于多个父级WI，也不允许成环
    role_to_session: Dict[str, str] # role到session的映射

    name: str # 名称 （<200字符）
    description: str # 描述(<2000字符)，2000字符都装不下则需要做设计文档了
    notes: str # 启动工作、完成、取消时填写，描述完成的情况
    is_review_wi: bool # 是不是review WI，对于非原子WI无效（也许应该存一个flag，然后getter合并检查是否是原子WI）
    project: Optional[str] # 属于某个project
```


# 典型嵌套形式

* write a website
  * write backend
    * database definition
    * write server
  * write frontend
    * main page
    * user management page
    * content page # 仅仅展开这一个来讲
      * 100: understand context, role: reviewer
      * 100: design, role: builder # 最前面的数字是 sequence_num
      * 200: design review, role: reviewer, is_review_wi: True
      * 300: coding, role, builder
      * 400: code review, role: reviewer, is_review_wi: True
      * 500: final review, role: user, is_review_wi: True

# tasks 中 workitems 序列

这个我们最基本的自动化流程，以content page workitem为例子。

* 对于这个content page WI，其有一系列的 tasks，按序号执行。
* 其中有三个role，分别是builder, reviewer, user。
  * 其中user role是特殊的保留名字，表示待用户手工完成，此处为final review，用户需要验收通过。
  * builder, reviewer是随便定的。对于每一个独立的role，会拉起一个session。如果属于某个project就在该project下启动新的自动session。

* 对于单个原子WI执行时，如果该WI还不存在对应role的session，则创建该role的auto session，后续该role的WI都由该auto session执行。
* 需要注意一个role session仅仅属于该WI，不同WI之间相同role的session不复用。
* tasks按sequence_num顺序执行
* 当多个task的sequence_num一样，且role不同时，可以并行执行
  * 同一个role对应多个sequence_num相同的task时，按wi_number顺序执行
* auto session工作完毕时，它调用工具关闭当前任务（标记为CLS），然后进入idle状态
* 当auto session被调度到一个新的原子WI时，该WI被标记为WRK
* 非原子WI不可执行，但是会递归执行所有task

# review 和 iteration

对于review WI（is_review_wi），当review通过时，行为类似完成一个普通WI。

当拒绝时，则触发iteration。会复制从上一个review开始的所有任务和review任务本身。后续任务的序号会增加。

例如本例子中400号review任务被拒绝，会变成这个样子：

* content page # 仅仅展开这一个来讲
  * 100: understand context, role: reviewer
  * 100: design, role: builder # 最前面的数字是 sequence_num
  * 200: design review, role: reviewer, is_review_wi: True
  * 300: coding, role, builder
  * 400: code review, role: reviewer, is_review_wi: True
  * 500: coding-iter 1, role, builder # 原名后加上"-iter (次数)"，次数采用正则匹配而来，匹配不到就从1开始，不增加存储负担
  * 600: code review-iter 1, role: reviewer, is_review_wi: True
  * 700: final review, role: user, is_review_wi: True // sequence_num增加了

# auto session

一种新的session类型，直接显示在session历史中，和用户手工创建的session近似。用户也可以和它谈话。

独有的工具：

* WI_complete
  * 完成工具，除了is_review_wi以外的工作结束的时候调用，调用后关闭该session（不将结果发送到API，但是正常保存结果以确保会话信息完整）
  * 参数：wi_number
  * 参数：notes: str
    - append到WI的notes字段
  * 返回："{wi_number} is closed as completed."
* WI_review_complete
  * review工具，当is_review_wi时调用
  * 参数：wi_number: int
  * 参数：passed: bool
    - 是否通过评审
  * 参数：notes
    - 为什么通过/不通过 
    - append到WI的notes字段
  * 返回："{wi_number} is closed as {passed | rejected}."
* WI_inspect
  * 参数：wi_number: int
  * 返回：WI的详情
    * 每个子WI显示：sequence_num, wi_number, status, role,


## 新任务

当auto session被赋予新任务的时候，调度器为它注入提示词。提示词以常规用户输入的形式输入。

形如：

```
<WI_scheduler>
Father WI details:
WI number: {父WI号}
Name: {父name}
Description: 
{父Description}
Tasks:
{每个子WI的 sequence_num, name, status, role}

---

Current Task Details:
WI number: {当前task WI号}
Name: {task WI名称}
Description:
{task description | None}
Notes: 
{task notes | None}
</WI_scheduler>
```

## 系统提示词

auto session的系统提示词在通用系统提示词之外增加：
* WBS工作流程的基本概念描述（WI/task递归、顺序、并行、状态）
* 它自己的role，role的描述
* 何时调用相应的WI工具
* 其他必要信息（TODO，需要设计）

# UI

当前创建栏长这样：`[+ Session][+ Chat][+ Project]`

增加WBS入口，改为：`[WorkItems][+ Session][+ Chat][+ Project]`

点击后，主视图进入WBS管理主视图。

顶部有两个Tab：主视图、树视图

## 主视图

主视图分为两栏，总表和待办

```
[+ WorkItem]
-----------------------
WI总表       | 待办
            |
            |
            |
            |
```

总表的布局：
```
Filters: [+]
WI表
```

Filters可以加这些过滤（点那个加号，出现编辑窗口，只支持AND逻辑）：
* Name Include(name: str)
* Status(status: enum)
* IsAtomic(is_atomic: bool)

每一个加上后，后面显示一个叉，可以点击去掉该filter，比如：[Status: ASN x]

