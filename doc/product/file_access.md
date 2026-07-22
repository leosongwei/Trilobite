# 文件访问权限

## 概述

Trilobite 的每个 session 有一个工作目录（working directory）。文件工具（`read`、`write`）在该目录范围内操作。工作目录外的访问需要通过额外授权目录（additional directories）机制显式开放。

`bash` 工具不强制路径限制（和 kimi-code 一致），通过系统提示词引导模型自觉遵守边界。

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
POST /api/sessions/{name}/dirs
Content-Type: application/json

{ "path": "/home/user/shared-libs" }
```

额外目录持久化在 `session.json` 中：

```json
{
  "name": "mysession",
  "working_dir": "/home/user/project",
  "additional_dirs": ["/home/user/shared-libs"],
  "plan_mode": false
}
```

添加后，`read`、`write` 工具可以自由访问该目录，与主工作目录无区别。

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
    │   └─ 否 -> 路径是否为绝对路径？
    │       ├─ 是 -> 允许访问（标记为 outside_workspace）
    │       └─ 否（相对路径逃逸） -> 拒绝，返回错误
```

**设计原则**（与 kimi-code 一致）：
- **相对路径逃逸被硬拒绝**：`../../etc/passwd` 这样的相对路径会被拒绝，强制使用绝对路径才能访问工作目录外的文件。这防止了意外的路径逃逸。
- **绝对路径被允许但标记**：`/etc/hosts` 这样的绝对路径可以通过检查，但被标记为 `outside_workspace`。目前不做额外限制（Trilobite 没有审批系统），但为未来引入审批机制预留了接口。

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

以下文件被**硬阻止**，无论是否在工作目录内，`read` 和 `write` 工具都拒绝访问：

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
| 工作目录外（相对路径逃逸） | 拒绝 |

### write 工具

与 read 工具相同的路径检查。在 Plan 模式下，额外检查：所有 `write` 调用被拒绝（Plan 模式守卫优先于路径检查）。

### bash 工具

**不强制路径限制。** bash 工具的 `cwd` 默认为 session 工作目录，但命令本身可以访问任何路径。

系统提示词引导模型：
> "除非用户明确指示，不要访问工作目录以外的文件。"

这与 kimi-code 的设计一致--bash 命令的路径检查在实践中不可行（命令可以是任意 shell 脚本），依赖模型自觉和用户监督。

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

### 路径逃逸被阻止

```
工作目录: /home/user/project

read("../../etc/passwd")      -> 拒绝：相对路径逃逸，请使用绝对路径
read("../../../home/user/.ssh/id_rsa")  -> 拒绝：相对路径逃逸
```

### 敏感文件被阻止

```
工作目录: /home/user/project

read(".env")                  -> 拒绝：敏感文件
read(".env.example")          -> 允许：在豁免列表中
read("/home/user/.ssh/id_rsa")  -> 拒绝：敏感文件
read("/home/user/.ssh/id_rsa.pub")  -> 允许：公钥在豁免列表中
write(".env.local", ...)      -> 拒绝：敏感文件
```

### 额外授权目录

```
工作目录: /home/user/project
额外目录: ["/home/user/shared-libs"]

read("/home/user/shared-libs/utils.py")    -> ✓ 在额外目录内
write("/home/user/shared-libs/new.py", ...)  -> ✓ 在额外目录内
read("/home/user/other/file.txt")          -> ✓ 绝对路径，允许（标记 outside_workspace）
read("../other/file.txt")                  -> 拒绝：相对路径逃逸
```

## 与 kimi-code 的对比

| 方面 | kimi-code | Trilobite |
|------|-----------|-----------|
| 边界定义 | workspaceDir + additionalDirs | working_dir + additional_dirs |
| 路径规范化 | 词法级别（不解析 symlink） | 相同 |
| 符号链接保护 | agent 工具层无，REST API 层有 | 暂无（未来可加） |
| 敏感文件 | 硬阻止 + 审批策略双层 | 硬阻止（单层） |
| 工作目录外访问 | 绝对路径允许但需审批 | 绝对路径允许（无审批系统） |
| bash 路径限制 | 无 | 无 |
| 额外目录添加 | CLI flag + slash command + config | API（未来可加 UI） |
| 审批系统 | 完整（manual/yolo/auto 模式 + 规则链） | 无（未来可加） |

## 未来扩展

1. **审批系统**：当工具访问工作目录外的路径时，通过 SSE 事件通知前端，用户审批后才执行（类似 plan 模式的审批流程）。
2. **符号链接保护**：在文件操作前调用 `os.path.realpath()` 解析符号链接，重新检查边界。
3. **用户权限规则**：类似 kimi-code 的 `config.toml` `[permission]` 段，允许用户配置 allow/deny/ask 规则。
4. **VCS 目录排除**：搜索工具（如果未来添加 grep/glob）自动排除 `.git`、`.svn` 等目录。
