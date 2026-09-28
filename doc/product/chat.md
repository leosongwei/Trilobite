# Chat（聊天模式）

## 概述

Chat 是一种特殊的普通 session：创建时忽略工作目录输入（即使填写也不生效），后端自动在 session 文件夹内创建 `chat_files/` 子目录并以其为该 session 的 `working_dir`。除工作目录的来源外，chat session 与普通 session 完全一致：同样的工具、权限、历史、压缩、subagent、文件管理器等行为；用户同样可以重命名、归属 project。

## 入口

Sidebar 顶部提供四个创建按钮：`[+ Session][+ Chat][+ Group][+ Project]`。`+ Session` 与 `+ Project` 复用顶部的工作目录输入框；`+ Chat` 不需要任何输入，点击即创建并选中。`+ Group` 创建群聊频道（见 [group.md](./group.md)），工作目录可填可不填。

## 数据与持久化

* `session.json` 中记录 `mode: "chat"` 字段（普通 session 无该字段）；创建请求中的 `working_dir` 被 chat 分支忽略。
* 工作目录为 `<session_dir>/chat_files/`，创建 session 时自动建目录。
* 初始名称为 "Chat"；首条消息后由自动命名器改写为消息摘要（与普通 session 相同，重命名后不覆盖）。
