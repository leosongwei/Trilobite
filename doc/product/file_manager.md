# 文件管理器

## 概述

文件管理器是一个面向用户的轻量文件浏览/编辑界面（类 IDE），与 agent 运行完全解耦：用户可以直接查看工作区文件、对比文件与某个 git 分支的差异、编辑并保存文件，无需经过 agent 的对话流程。文件树只对**主 session** 提供（subagent 视图不显示）。

三个核心能力（issue #49）：

1. **查看 diff**：文件与指定 git 分支（默认 `master`）的差异，复用聊天里的 `DiffView` 组件渲染（分屏/统一双视图）。
2. **编辑文件**：textarea 全文件编辑，保存直接落盘，不受 agent 运行状态和 plan mode 限制。
3. **代码高亮**：只读查看模式用 highlight.js 渲染语法高亮（依赖小、易集成；若集成成本过高可砍掉，不影响前两项）。

## 入口与布局

* 文件树**内嵌在 sidebar**（`SessionSidebar` 底部，"Session files" 区域，仅主 session），默认展开工作区根目录——无需单独打开文件管理器即可浏览文件、查看 vs 分支的改动高亮。点击文件在 main 区域打开文件内容视图（顶栏提供"返回对话"按钮）。
* sidebar 中 session 列表与下方面板（会话信息 + 文件树）之间有一条**可上下拖拽的分划线**，调整两部分高度。
* 切换会话时自动关闭文件视图回到对话，文件树随之重建。
* 文件视图布局：顶栏（返回对话、文件路径、base 分支下拉（仅 diff 模式）、查看/Diff 切换（segmented tabs）、编辑/保存/取消按钮）+ 内容区（只读高亮视图或 `DiffView` 或编辑 textarea）。

## 后端

### 新增模块 `src/trilobite/git_ops.py`

轻量 git 封装（与 `file_discovery.py` 同风格，subprocess 调用、无第三方依赖）：

* `is_git_repo(root)` → bool（`git rev-parse --is-inside-work-tree`）
* `list_dir(root, relpath)` → 单个目录的条目清单 + git 状态：
  * git 仓库：`git ls-files -- <dir>` 取 tracked 文件 + `git ls-files -o --exclude-standard -- <dir>` 取未跟踪非忽略文件（被 .gitignore 忽略的目录如 `.venv`/`node_modules` 天然不出现），再配合 `git status --porcelain -- <dir>` 标记 `modified/added/deleted/untracked`，其余为 `clean`
  * 非 git 仓库：`os.listdir`（复用 `file_discovery._NOISE_DIRS` 剪枝噪音目录），全部标记 `untracked`
* `list_branches(root)` → 分支名列表 + 当前分支（`git for-each-ref refs/heads` + `git symbolic-ref --short HEAD`）
* `show_base_content(root, base, relpath)` → `git show <base>:<relpath>` 的基线内容；base 分支不存在或文件在基线中不存在时返回 None + 原因

### 新增端点（`src/trilobite/server.py`）

所有端点挂在 `/api/sessions/{name}/...` 下，受现有 auth 中间件保护。路径参数一律传**绝对路径**，统一走 `file_access.resolve_file_path`（working_dir + additional_dirs 边界检查、敏感文件过滤），范围外或敏感文件直接返回错误（**不弹权限横幅**——这是用户主动 UI 操作，不是 agent 请求）。

| 方法 | 路径 | 用途 |
|---|---|---|
| GET | `/api/sessions/{name}/fs/list?path=<dir>` | 列单个目录的内容（懒加载，展开目录时按需请求） |
| GET | `/api/sessions/{name}/fs/file?path=` | 读文件全文 |
| GET | `/api/sessions/{name}/fs/diff?path=&base=master` | 文件 vs 分支的 diff（`DiffRow[]`） |
| PUT | `/api/sessions/{name}/fs/file` | 保存文件（body: `{path, content}`） |

#### `GET /fs/list?path=<dir>`

```json
{
  "path": "/home/user/project",
  "name": "project",
  "is_git_repo": true,
  "current_branch": "feat/file-manager",
  "branches": ["master", "feat/file-manager"],
  "entries": [
    { "name": "src", "is_dir": true },
    { "name": "main.py", "is_dir": false, "size": 1024, "mtime": 1710000000, "status": "modified" }
  ],
  "truncated": false
}
```

* `path` 是绝对目录路径，根为 working_dir 或某个 additional_dir（文件树初始只请求各根，展开子目录时再请求该目录）。
* `entries` 是**该目录的直接子项**（含子目录与文件），不递归——避免一次性拉全量文件树（`.venv`/`node_modules` 等大目录不会在未展开时传输，且 git 仓库中被 ignore 的目录直接不出现）。
* `status`：`clean | modified | added | deleted | untracked`（deleted 仅 git 仓库中出现，前端灰色显示，点击提示文件已删除）。
* 单目录条目数超过 5000 时截断并置 `truncated: true`（提示目录过大），防止意外卡死前端。

#### `GET /fs/file?path=`

* 返回 `{"content": "..."}` 全文。
* 二进制检测（内容含 NUL 字节）→ 400 "二进制文件"；超过 512 KB → 413 "文件过大"（提示用 agent 的 `read` 工具分页）。
* 敏感文件被 `resolve_file_path` 拒绝（与 agent 工具行为一致）。

#### `GET /fs/diff?path=&base=master`

* 非 git 仓库 → 400 "不是 git 仓库"；base 分支不存在 → 400 "分支不存在"。
* 基线内容 = `git show <base>:<relpath>`；文件在基线中不存在（untracked/added）→ 基线为空，diff 为全 added。
* 当前内容（工作区）vs 基线内容，用 `difflib.SequenceMatcher(autojunk=False)` 全文件对比生成 `DiffRow[]`（`{type: equal|added|removed, old, new, text}`，1-based 行号，与 `tools/edit.py` 的 diff 格式一致，前端 `DiffView.vue` 零改动直接消费）。
* diff 行数超过 20000 拒绝（413 "diff too large"），防止前端渲染卡死。
* 文件同样受二进制/大小限制。
* 返回 `{"rows": [...], "base": "master", "untracked": false}`。

#### `PUT /fs/file`（body: `{path, content}`）

* 直接落盘，**不走 agent**：文件管理器是用户自己的 IDE 操作，不产生对话历史、不经过 permission 拦截、不受 plan mode 限制（plan mode 只约束 agent 的工具调用，用户手动改文件本来就是允许的）、不受 agent 运行状态阻塞。
* 行尾处理：读取时检测原文件行尾（CRLF/LF），保存时把提交内容还原为原行尾写回（复用 `tools/edit.py` 的 `_detect_line_ending`/`_materialize` 逻辑，提取为公共函数）。
* 父目录不存在 → 400；敏感文件 → 拒绝；working_dir/additional_dirs 之外 → 拒绝。
* 返回 `{"ok": true}`。

## 前端

### 新组件

* `FileManager.vue` — main 区域的**文件内容视图**（无自己的树）：顶栏（返回对话、base 分支下拉、查看/Diff/编辑 tabs、保存/取消）+ 内容区（highlight.js 只读高亮 / `DiffView` / textarea 编辑）。打开的文件由 props 从 sidebar 树传入；切换文件保持当前视图模式，diff 模式打开文件时预加载内容。保存后 emit `file-saved` 通知 sidebar 刷新树。数据用组件内 ref 管理，**不进全局 store**。
* `FileTree.vue` — 递归渲染目录树：**懒加载**（展开目录时请求该目录内容并缓存，已加载目录内容缓存在组件内避免重复请求）、文件按 git 状态显示徽章（M/A/[U]/D）并按状态高亮文件名（modified 黄、added 绿、untracked 蓝、deleted 红删除线）、**diff 模式**（props `base`）下相对所选分支有改动的文件按同样规则着色（目录标黄，递归标记，未展开也能看到）；`roots` 变化（切 session/增删目录）时重建。会改文件的工具（edit/write/bash/task）调用返回时，以及 subagent 更新、run 结束，自动重新加载所有已展开目录（保留展开状态，防抖合并），agent 创建/修改的文件即时出现在树中；read/glob/grep/todo 等只读工具返回与仅出新助手消息时不刷新。
* diff 复用现有 `DiffView.vue`（props `rows: DiffRow[]`，含 split/unified 响应式切换）。
* 编辑模式：原生 textarea（全文件、等宽字体），Ctrl+S 保存、Esc 取消，未保存切换文件/视图时确认提示；保存成功后刷新 sidebar 树并回到查看模式。
* 查看模式：只读渲染 + highlight.js 高亮（语言按扩展名推断；未识别语言回落为纯文本）。
* 默认模式：git 工作区打开文件即 **diff 模式**（vs 默认 `master` 分支），非 git 工作区自动落查看模式。

### `api.ts` 新增

`getFileList(id, path)` / `getFileContent(id, path)` / `getFileDiff(id, path, base)` / `saveFile(id, path, content)`，走现有 `authFetch`。

### 依赖

`highlight.js` 加入前端 dependencies（全量语言包，体积可接受；若嫌大可改为按需注册常用语言）。

## 边界情况

| 情况 | 行为 |
|---|---|
| 非 git 仓库 | 目录列表正常（`os.listdir` + 噪音目录剪枝），diff 按钮禁用（后端 400 "不是 git 仓库"），分支下拉隐藏 |
| git 仓库中被 ignore 的目录（`.venv`/`node_modules` 等） | 目录列表不出现（`git ls-files` 尊重 .gitignore），即使展开也不会传输 |
| base 分支不存在 | 下拉里只有真实存在的分支，不会出现 |
| untracked / added 文件 | diff 显示全 added（基线为空） |
| deleted 文件 | 树中灰色显示，点击提示"文件已删除，无法查看" |
| 单目录条目超过 5000 | 截断并提示"目录过大"（`truncated: true`） |
| 二进制文件 | 拒绝读取/编辑/diff，提示"二进制文件" |
| 超过 512 KB | 拒绝读取/编辑/diff，提示用 agent 的 `read` 工具分页查看 |
| 敏感文件（.env 等） | 树中显示但读取/保存被后端拒绝 |
| working_dir / additional_dirs 之外 | 后端拒绝（不会出现在树中，除非手动构造请求） |
| agent 运行中编辑文件 | 允许；用户自行承担与 agent 读取的时序一致性（与终端里手动改文件同等语义） |
| subagent 会话 | 无文件树 |

## 代码高亮取舍

只读查看模式使用 highlight.js；若构建体积或集成成本超出预期，可移除高亮只保留纯文本查看，不影响 diff 与编辑两个核心能力（需求方已确认）。
