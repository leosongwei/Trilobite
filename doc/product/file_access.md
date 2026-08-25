# 文件访问权限

## 概述

Trilobite 的每个 session 有一个工作目录（working directory）。文件工具（`read`、`glob`、`grep`、`edit`、`write`）在该目录范围内操作。工作目录外的访问需要通过额外授权目录（additional directories）机制显式开放。

`bash` 工具在 Linux 上默认运行在 bubblewrap（`bwrap`）沙箱中：整个文件系统只读挂载，仅工作目录和已授权目录（additional directories）可写。这样 bash 无法在工作区外写入文件，与文件工具的边界一致（详见下文「bash 沙箱」）。

为了让模型知道边界在哪、从而优先用相对路径工作而不是猜测绝对路径飘到工作目录外，Agent 会在 system 消息最前面注入一个动态的 `<env>` 块，给出工作目录的绝对路径、是否 git 仓库、平台等信息（详见 [context_building.md](./context_building.md)）。系统提示词的 Permissions 段也指示模型默认在工作目录内用相对路径工作，仅在任务确实需要时才用绝对路径访问外部并走授权流程。

## 工作目录边界

### 边界定义

每个 session 的工作目录在创建时指定，存储在 `session.json` 中：

```json
{ "name": "mysession", "working_dir": "/home/user/project", "plan_mode": false }
```

文件工具的所有路径操作都基于此目录进行规范化（canonicalize）和边界检查。

### 额外授权目录

用户可以为 session 添加额外的工作目录，使其与主工作目录享有同等访问权限：

```http
POST /api/sessions/{id}/dirs
Content-Type: application/json

{ "path": "/home/user/shared-libs" }
```

`{id}` 是 session 的稳定标识（UUID 目录名），不是 `session.json` 里的人可读 `name`。额外目录持久化在 `session.json` 中：

```json
{
  "name": "mysession",
  "working_dir": "/home/user/project",
  "additional_dirs": ["/home/user/shared-libs"],
  "plan_mode": false
}
```

添加后，`read`、`edit`、`write` 工具可以自由访问该目录，与主工作目录无区别。

**规范化存储**：`additional_dirs` 以规范化形式（`~` 展开、符号链接解析、相对路径基于工作目录解析，见 `normalize_dir`）持久化。手工添加 `/foo/`、`/foo`、`~/foo` 会被折叠为同一条 `/foo`，字符串级去重因此始终有效；历史遗留的 `/foo/` 写法在下次添加/删除时被自动改写。

**组级语义**：子 agent（subagent）在 spawn 时继承主 session 的授权目录；主 session 的授权目录在运行中发生变化（手工添加、权限审批）时，会同步传播到所有运行中的子 agent（保留子 agent 自己单独获批的目录），因此已经对主 session 授权过的目录不会再向子 agent 重复请求权限。侧边栏 Allowed directories 展示的是整个组（主 session + 子 agent）按路径去重后的并集，同一目录只出现一次。

## 路径规范化与边界检查

### 规范化流程

文件工具收到路径参数后，执行以下步骤：

1. **展开**：`~` 展开为用户 home 目录
2. **规范化**：如果是相对路径，基于工作目录拼接；然后词法规范化（解析 `..` 和 `.`，不访问文件系统）
3. **敏感文件检查**：检查规范化后的路径是否匹配敏感文件模式（详见下文）
4. **边界检查**：检查路径是否在工作目录或额外授权目录内

### 边界检查规则

```
规范化后的路径
    │
    ├─ 在工作目录或额外目录内？
    │   ├─ 是 -> 允许访问
    │   └─ 否 -> 触发权限请求（暂停 Agent，前端弹窗提示用户 Grant/Deny）
```

**设计原则**：
- **白名单制**：所有路径（绝对或相对）必须在 `working_dir` 或 `additional_dirs` 内。
- **权限提示而非硬拒绝**：当模型尝试访问工作区外的路径时，Agent 暂停执行，前端弹出横幅让用户授权（Grant）或拒绝（Deny）。批准后目录自动加入 `additional_dirs` 并持久化，Agent 自动重试工具调用。用户无需手动添加目录。
- **敏感文件硬拒绝**：`.env`、SSH 密钥等敏感文件无论位置一律拒绝，不走权限提示流程。

### 权限请求 UI（横幅 + Pending Requests 列表）

所有待审批请求（目录授权、切换到 build 模式）统一进入前端 **pending requests 列表**，同一主会话组（主 session + 其子 agent）内的请求互不覆盖、各自独立审批：

- **横幅**：当前浏览的 session 所属主会话组内有 pending 请求时弹出。只有一个请求时显示详情与 Approve/Reject（目录授权显示 Grant/Deny）；多个请求并发时聚合为 "N permission requests are pending" + Review 按钮（打开侧边栏 Pending Requests 列表）。主 agent 与子 agent 的请求都 fan-out 到整个主会话组的 broker（事件带 `session` 字段标明请求方），浏览组内任意 session 都能看到。
- **Pending Requests 列表**：侧边栏 "Allowed directories" 下方可展开，列出全部 pending 请求（含请求方、路径/模式切换、Approve/Reject 按钮）。批准后条目消失；目录授权批准的路径进入该 session 的 Allowed directories，横幅同步消失。

请求被批准/拒绝后条目即从列表移除；请求方 session 停止运行（未答复即结束）时条目自动清理。

### Allowed directories 的组内展示

Allowed directories 本身是 per-session 的（子 agent 批准的目录只写入该子 session，不传播给父或兄弟）。侧边栏展示时按**当前主会话组**合并：浏览组内任意 session 都能看到主 session + 全部子 agent 的授权目录并集，非当前浏览 session 的条目标注来源（`[type: description]` 或 session 名），可删除。`+` 添加框只作用于当前浏览的 session。

### 共享前缀攻击防护

边界检查使用路径分隔符感知的前缀匹配，避免 `/home/user/project-evil` 通过 `/home/user/project` 的前缀检查：

```python
def is_within_directory(candidate: str, base: str) -> bool:
    if candidate == base:
        return True
    prefix = base if base.endswith('/') else base + '/'
    return candidate.startswith(prefix)
```

### 符号链接

当前实现是**词法级别**的（不解析符号链接）。这意味着工作目录内的符号链接如果指向外部路径，不会被拦截。这与 kimi-code 的 agent 工具层一致。

未来如果需要更强的安全保障，可以在文件操作前调用 `os.path.realpath()` 解析符号链接并重新检查边界。

## 敏感文件保护

### 硬阻止列表

以下文件被**硬阻止**，无论是否在工作目录内，`read`、`edit` 和 `write` 工具都拒绝访问：

| 类别 | 匹配规则 | 示例 |
|------|----------|------|
| 环境变量文件 | 文件名为 `.env` 或 `.env.*` | `.env`、`.env.local`、`.env.production` |
| SSH 私钥 | 文件名为 `id_rsa`、`id_ed25519`、`id_ecdsa` | `~/.ssh/id_rsa` |
| 云平台凭证 | 路径包含 `.aws/credentials` 或 `.gcp/credentials` | `~/.aws/credentials` |
| 通用凭证文件 | 文件名为 `credentials` | `credentials` |

### 豁免列表

以下文件不被视为敏感文件：

| 类别 | 匹配规则 | 示例 |
|------|----------|------|
| 环境变量模板 | `.env.example`、`.env.sample`、`.env.template` | `.env.example` |
| SSH 公钥 | `*.pub` 后缀 | `id_rsa.pub`、`id_ed25519.pub` |

### 变体重命名防护

攻击者可能通过重命名绕过检查（如 `id_rsa.bak`、`id_rsa-old`）。以下后缀变体也被阻止：

```
.bak、.backup、.copy、.disabled、.key、.old、.orig、.pem、.save、.tmp
```

以及连字符和下划线变体：`id_rsa-old`、`id_rsa_old`。

### 大小写不敏感

所有匹配大小写不敏感（兼容 Windows）。

## 各工具的权限行为

### read 工具

| 检查项 | 行为 |
|--------|------|
| 路径规范化 | 相对路径基于工作目录解析 |
| 敏感文件 | 硬阻止，返回错误 |
| 工作目录内 | 允许 |
| 工作目录外（绝对路径） | 允许（标记 outside_workspace） |
| 工作目录外 | 权限提示（Grant/Deny） |

### edit 工具

与 read 工具相同的路径检查。对已有文件做精确字符串替换：`old_string` 必须在文件中唯一（或设 `replace_all` 替换全部）。工具内部对纯 CRLF 文件做行尾归一化--在 LF「模型视图」上匹配（与 read 工具展示的 LF 视图一致），写回时还原原始 CRLF，从而避免 LLM 提供的 LF `old_string` 在 CRLF 文件上匹配失败。在 Plan 模式下，所有 `edit` 调用被拒绝（Plan 模式守卫优先于路径检查）。

### write 工具

与 read 工具相同的路径检查。整文件创建/覆盖/追加（`mode` 为 `overwrite` 或 `append`），原样写入、不做匹配。在 Plan 模式下，所有 `write` 调用被拒绝（Plan 模式守卫优先于路径检查）。

### glob 工具

按文件名 glob 模式查找文件（如 `**/*.py`），返回匹配路径（相对工作目录），按修改时间倒序排列。路径检查与 read 一致：`path` 参数（搜索根目录）经 `resolve_file_path` 解析，工作目录外需授权。在 git 仓库中通过 `git ls-files` 尊重 `.gitignore`（见 `file_discovery.py`），非 git 目录则遍历并跳过常见噪音目录（`.git`、`node_modules`、`__pycache__` 等）。只读工具，Plan 模式可用。

### grep 工具

按正则搜索文件内容，返回 `path:line: content` 格式的匹配行。支持 `output_mode`（`content` / `files_with_matches` / `count`）、`glob` 文件名过滤、`context` 上下文行、`case_insensitive`、`max_results`。路径检查与 read 一致；文件发现与 glob 工具共用 `file_discovery.discover_files`，同样尊重 `.gitignore`。自动跳过二进制文件（含 NUL 字节）。只读工具，Plan 模式可用。

### bash 工具

**沙箱隔离（Linux）。** bash 工具默认运行在 bubblewrap 沙箱中（config 的 `bash_sandbox` 键：`auto` 为默认，探测到 `bwrap` 可用就启用，否则裸跑并在结果中提示；`on` 强制启用，不可用则拒绝执行；`off` 关闭沙箱）。沙箱挂载布局：

```
bwrap --ro-bind / / --dev /dev --proc /proc \
      --bind /dev/shm /dev/shm --bind <session_dir>/tmp /tmp \
      --bind <working_dir> <working_dir> \
      [--bind <additional_dir> <additional_dir> ...] \
      --die-with-parent -- bash -c <command>
```

整个文件系统只读，仅工作目录、已授权目录（additional directories，与文件工具的授权集一致）和会话的 scratch 空间可写；`/tmp` 挂载 session 文件夹下的 `tmp/` 目录（不存在时自动创建；创建失败则整个 run 中止并广播 error 事件——与模型 API 请求失败同语义，由用户处理 session 目录的权限问题），每个会话独立、在会话内跨 bash 调用持久；`/dev/shm` 继承宿主。可写目录中缺失或不存在的路径会被跳过（bwrap 要求挂载目标存在）。沙箱探测结果在进程内缓存。`bwrap` 不可用或非 Linux 平台时，bash 退化为普通执行（`auto` 模式带提示；`on` 模式直接拒绝），行为与未启用沙箱一致。

**被拒反馈。** 沙箱拦截工作区外写入时内核返回只读文件系统错误，工具结果会附带 `[sandbox]` 提示，说明写入被沙箱阻止、并引导模型先用 `read` 工具读取目标文件以发起权限请求——用户批准后该目录加入 additional directories，bash 沙箱随即允许写入（复用现有审批流程，无新机制）。

**输出截断。** 命令输出默认截断为**尾部** 100 行 / 10000 字符（双限制--bash 输出尾部通常含错误信息和最终结果）。模型可通过 `max_output_lines` / `max_output_chars` 调整，传 `-1` 关闭对应限制（两者都为 `-1` 时返回完整输出）。截断发生时在输出开头插入提示行，引导模型按需放宽限制或分页查看。

**调用描述。** `description` 为必选参数，模型需用主动语态简明描述本次调用目的（5-10 词，示例："List files in current directory"），前端展示在 bash 命令上方。

系统提示词引导模型：
> "除非用户明确指示，不要访问工作目录以外的文件。"

沙箱保证的是**写入边界**（与文件工具一致）；读取工作区外文件在沙箱内仍然允许（只读挂载），由文件工具的敏感文件保护（见上）和用户审批约束。

### TodoList 工具

操作 session 目录下的 `todos.json`，不涉及工作目录路径检查。

## 实际例子

### 正常访问

```
工作目录: /home/user/project

read("src/main.py")           -> /home/user/project/src/main.py  ✓ 工作目录内
read("/etc/hostname")         -> /etc/hostname                    ✓ 绝对路径，允许
write("src/new.py", ...)      -> /home/user/project/src/new.py   ✓ 工作目录内
```

### 权限提示（工作区外访问）

```
工作目录: /home/user/project

read("/etc/passwd")           -> 权限提示：Agent 暂停，用户 Grant/Deny
read("../other/file.txt")     -> 权限提示（规范化后在工作区外）
```

### 敏感文件被阻止

```
工作目录: /home/user/project

read(".env")                  -> 硬拒绝：敏感文件
read(".env.example")          -> 允许：在豁免列表中
read("/home/user/.ssh/id_rsa")  -> 硬拒绝：敏感文件
read("/home/user/.ssh/id_rsa.pub")  -> 允许：公钥在豁免列表中
write(".env.local", ...)      -> 硬拒绝：敏感文件
```

### 额外授权目录（无权限提示）

```
工作目录: /home/user/project
额外目录: ["/home/user/shared-libs"]

read("/home/user/shared-libs/utils.py")    -> ✓ 直接允许
write("/home/user/shared-libs/new.py", ...)  -> ✓ 直接允许
read("/home/user/other/file.txt")          -> 权限提示（不在额外目录中）
```

## 与 kimi-code 的对比

| 方面 | kimi-code | Trilobite |
|------|-----------|-----------|
| 边界定义 | workspaceDir + additionalDirs | working_dir + additional_dirs |
| 路径规范化 | 词法级别（不解析 symlink） | 相同 |
| 符号链接保护 | agent 工具层无，REST API 层有 | 暂无（未来可加） |
| 敏感文件 | 硬阻止 + 审批策略双层 | 硬阻止（单层） |
| 工作目录外访问 | 绝对路径允许但需审批 | 权限提示（Grant/Deny），批准后自动加入白名单 |
| bash 路径限制 | 无 | bwrap 沙箱（Linux，工作区 + 授权目录可写） |
| 额外目录添加 | CLI flag + slash command + config | 权限提示自动添加 + API |
| 审批系统 | 完整（manual/yolo/auto 模式 + 规则链） | 已实现（SSE 事件暂停 + 前端横幅） |

## 未来扩展

1. **符号链接保护**：在文件操作前调用 `os.path.realpath()` 解析符号链接，重新检查边界。
2. **用户权限规则**：类似 kimi-code 的 `config.toml` `[permission]` 段，允许用户配置 allow/deny/ask 规则。
3. **VCS 目录排除**：搜索工具（如果未来添加 grep/glob）自动排除 `.git`、`.svn` 等目录。
